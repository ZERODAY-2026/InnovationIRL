"""Dataset profiles: the YAML semantic layer (tables, joins, metrics, synonyms, rules) behind every prompt and check.

A new dataset needs a new profiles/<name>.yaml and setup SQL, not code.
"""
from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from rapidfuzz import fuzz

from .config import ROOT

PROFILES_DIR = ROOT / "profiles"
CACHE_DIR = PROFILES_DIR / "cache"  # generated from Snowflake at setup time (git-ignored)
_JOIN_RE = re.compile(r"(\w+)\.(\w+)\s*=\s*(\w+)\.(\w+)")
_WORD_RE = re.compile(r"[a-z0-9#]+")
MAX_INLINE_VALUES = 30


class ProfileError(ValueError):
    """The profile YAML is inconsistent (unknown table, column or join)."""


@dataclass
class Column:
    name: str
    type: str
    desc: str = ""
    values: list[str] | None = None
    auto_values: bool = False
    hidden: bool = False
    format: str | None = None


@dataclass
class Table:
    name: str
    description: str
    columns: dict[str, Column]
    aliases: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class JoinEdge:
    left_table: str
    left_col: str
    right_table: str
    right_col: str

    def __str__(self) -> str:
        return f"{self.left_table}.{self.left_col} = {self.right_table}.{self.right_col}"


@dataclass
class Metric:
    name: str
    sql: str
    desc: str = ""
    synonyms: list[str] = field(default_factory=list)
    format: str | None = None


@dataclass
class Profile:
    name: str
    title: str
    description: str
    database: str
    schema: str
    cloud_allowed: bool
    max_rows: int
    time_anchor: str | None
    tables: dict[str, Table]
    joins: list[list[JoinEdge]]  # one inner list per declared relationship (composite joins have several edges)
    metrics: dict[str, Metric]
    synonyms: dict[str, str]
    rules: list[str]
    default_paths: list[list[str]] = field(default_factory=list)  # preferred join routes, e.g. sales -> customer nation
    examples: list[dict] = field(default_factory=list)  # [{"question": ..., "plan": {...}}] shown to the planner
    coverage: dict[str, tuple[str, str]] = field(default_factory=dict)  # "TABLE.COL" -> (min, max)
    _domain: dict[tuple[str, str], tuple[str, str]] = field(default_factory=dict, repr=False)

    # ---- structure -------------------------------------------------------
    def schema_mapping(self) -> dict[str, dict[str, str]]:
        """TABLE -> COLUMN -> SQL type, for sqlglot column validation. Hidden columns are not available to queries."""
        sql_type = {"number": "DOUBLE", "text": "VARCHAR", "date": "DATE", "timestamp": "TIMESTAMP", "boolean": "BOOLEAN"}
        return {t.name: {c.name: sql_type.get(c.type, "VARCHAR") for c in t.columns.values() if not c.hidden}
                for t in self.tables.values()}

    def edges(self) -> list[JoinEdge]:
        return [edge for group in self.joins for edge in group]

    def is_key(self, table: str, column: str) -> bool:
        return (table, column) in self._domain

    def same_key_domain(self, t1: str, c1: str, t2: str, c2: str) -> bool:
        """True when two key columns hold the same kind of key (e.g. C_NATIONKEY and S_NATIONKEY)."""
        if (t1, c1) == (t2, c2):
            return True
        return self._find((t1, c1)) == self._find((t2, c2))

    def table_graph(self) -> dict[str, set[str]]:
        graph: dict[str, set[str]] = {name: set() for name in self.tables}
        for e in self.edges():
            graph[e.left_table].add(e.right_table)
            graph[e.right_table].add(e.left_table)
        return graph

    def money_columns(self) -> set[str]:
        cols = {c.name for t in self.tables.values() for c in t.columns.values() if c.format == "money"}
        return cols | {m.name.upper() for m in self.metrics.values() if m.format == "money"}

    def _find(self, node: tuple[str, str]) -> tuple[str, str]:
        while self._domain[node] != node:
            self._domain[node] = self._domain[self._domain[node]]
            node = self._domain[node]
        return node

    def _union(self, a: tuple[str, str], b: tuple[str, str]) -> None:
        self._domain.setdefault(a, a)
        self._domain.setdefault(b, b)
        ra, rb = self._find(a), self._find(b)
        if ra != rb:
            self._domain[rb] = ra

    # ---- prompt support ----------------------------------------------------
    def relevant_tables(self, question: str) -> list[str]:
        """Tables the question seems to need, plus the tables that connect them. Used only to shrink prompts."""
        q = question.lower()
        words = set(_WORD_RE.findall(q))
        picked: set[str] = set()

        def mentioned(term: str) -> bool:
            term = term.lower().strip()
            if not term:
                return False
            if " " in term or "#" in term:
                return term in q
            return any(fuzz.ratio(term, w) >= 88 for w in words if abs(len(w) - len(term)) <= 3)

        for t in self.tables.values():
            terms = [t.name, t.name.rstrip("S"), *t.aliases]
            for c in t.columns.values():
                if not c.hidden and c.values:
                    terms.extend(v for v in c.values if len(v) >= 4)
            if any(mentioned(term) for term in terms):
                picked.add(t.name)
        for term, target in self.synonyms.items():
            if mentioned(term):
                picked.add(target.split(".")[0])
        for m in self.metrics.values():
            if any(mentioned(term) for term in [m.name.replace("_", " "), *m.synonyms]):
                picked.update(self._tables_in(m.sql))
        if not picked:
            return list(self.tables)
        return self._connect(picked)

    def value_hints(self, question: str, limit: int = 6) -> list[str]:
        """Grounded literal hints: likely typos of listed values, and words found inside long (not inlined) lists."""
        words = re.findall(r"[A-Za-z0-9#]+", question)
        grams = {" ".join(words[i:i + n]) for n in (1, 2, 3) for i in range(len(words) - n + 1)}
        lowered = question.lower()
        hints: list[tuple[int, str]] = []
        seen: set[str] = set()
        for table in self.tables.values():
            for column in table.columns.values():
                if column.hidden or not column.values:
                    continue
                target = f"{table.name}.{column.name}"
                if len(column.values) <= MAX_INLINE_VALUES:
                    for value in column.values:
                        if len(value) < 4 or value.lower() in lowered:
                            continue  # exact values are already visible in the prompt
                        for gram in grams:
                            score = fuzz.ratio(gram.lower(), value.lower())
                            if len(gram) >= 4 and score >= 88 and value not in seen:
                                hints.append((int(score), f'"{gram}" probably means {target} = \'{value}\''))
                                seen.add(value)
                else:
                    for word in {w.upper() for w in words if len(w) >= 4}:
                        matches = [v for v in column.values if word in re.split(r"[\s\-]+", v.upper())]
                        if matches and (target, word) not in seen:
                            seen.add((target, word))  # type: ignore[arg-type]
                            examples = ", ".join(f"'{m}'" for m in matches[:3])
                            hints.append((90, f'"{word.lower()}" appears in {len(matches)} {target} values, e.g. {examples}; '
                                              f"filter with {column.name} ILIKE '%{word}%'"))
        return [text for _, text in sorted(hints, key=lambda h: -h[0])[:limit]]

    def render_schema(self, tables: list[str] | None = None, with_values: bool = True) -> str:
        """Compact schema text for the PLAN prompt. Hidden columns are omitted."""
        names = [n for n in self.tables if tables is None or n in tables]
        lines = [f"Snowflake tables in {self.database}.{self.schema}:"]
        for name in names:
            t = self.tables[name]
            cols = []
            for c in t.columns.values():
                if c.hidden:
                    continue
                item = f"{c.name} {c.type}"
                if c.desc:
                    item += f" ({c.desc})"
                rng = self.coverage.get(f"{name}.{c.name}")
                if rng:
                    item += f" [{rng[0]}..{rng[1]}]"
                if with_values and c.values and len(c.values) <= MAX_INLINE_VALUES:
                    item += " {" + "|".join(c.values) + "}"
                cols.append(item)
            lines.append(f"{name}: {t.description}\n  " + "; ".join(cols))
        edges = [str(e) for e in self.edges() if e.left_table in names and e.right_table in names]
        if edges:
            lines.append("Joins (use only these): " + "; ".join(edges))
        metrics = [m for m in self.metrics.values() if self._tables_in(m.sql) <= set(names)]
        if metrics:
            lines.append("Metrics:")
            for m in metrics:
                aka = f" (also: {', '.join(m.synonyms)})" if m.synonyms else ""
                lines.append(f"  {m.name} = {m.sql} -- {m.desc}{aka}".rstrip(" -"))
        syn = [f"{k} -> {v}" for k, v in self.synonyms.items() if v.split(".")[0] in names]
        if syn:
            lines.append("Synonyms: " + "; ".join(syn))
        if self.rules:
            lines.append("Rules:\n" + "\n".join(f"- {r}" for r in self.rules))
        return "\n".join(lines)

    def _tables_in(self, sql: str) -> set[str]:
        found = set()
        for token in re.findall(r"\b[A-Z][A-Z0-9_]*\b", sql.upper()):
            for t in self.tables.values():
                if token in t.columns:
                    found.add(t.name)
        return found

    def _connect(self, picked: set[str]) -> list[str]:
        """Add the tables that connect the picked ones: a declared default path if one covers both, else the shortest."""
        graph = self.table_graph()
        result = set(picked)
        anchor = sorted(picked)[0]
        for target in picked:
            result.update(self._default_segment(anchor, target) or _shortest_path(graph, anchor, target))
        return [n for n in self.tables if n in result]

    def _default_segment(self, a: str, b: str) -> list[str] | None:
        for path in self.default_paths:
            if a in path and b in path:
                i, j = sorted((path.index(a), path.index(b)))
                return path[i : j + 1]
        return None


def _shortest_path(graph: dict[str, set[str]], start: str, goal: str) -> list[str]:
    if start == goal:
        return [start]
    prev: dict[str, str | None] = {start: None}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        for nxt in sorted(graph[node]):
            if nxt not in prev:
                prev[nxt] = node
                if nxt == goal:
                    path = [goal]
                    while prev[path[-1]] is not None:
                        path.append(prev[path[-1]])  # type: ignore[arg-type]
                    return path[::-1]
                queue.append(nxt)
    return [start, goal]  # not connected: keep both, the gate will reject a bad join


def load_profile(name: str, profiles_dir: Path | None = None, cache_dir: Path | None = None) -> Profile:
    base = profiles_dir or PROFILES_DIR
    path = base / f"{name}.yaml"
    if not path.exists():
        raise ProfileError(f"profile '{name}' not found at {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    tables: dict[str, Table] = {}
    for tname, tdef in (raw.get("tables") or {}).items():
        tname = tname.upper()
        cols = {}
        for cname, cdef in (tdef.get("columns") or {}).items():
            cdef = cdef or {}
            values = cdef.get("values")
            cols[cname.upper()] = Column(
                name=cname.upper(),
                type=str(cdef.get("type", "text")),
                desc=str(cdef.get("desc", "")),
                values=[str(v) for v in values] if isinstance(values, list) else None,
                auto_values=values == "auto",
                hidden=bool(cdef.get("hidden", False)),
                format=cdef.get("format"),
            )
        tables[tname] = Table(tname, str(tdef.get("description", "")), cols, [str(a) for a in tdef.get("aliases", [])])

    def check_col(t: str, c: str, where: str) -> None:
        if t not in tables or c not in tables[t].columns:
            raise ProfileError(f"{where}: unknown column {t}.{c}")

    joins: list[list[JoinEdge]] = []
    for spec in raw.get("joins") or []:
        group = []
        for lt, lc, rt, rc in _JOIN_RE.findall(str(spec).upper()):
            check_col(lt, lc, f"join '{spec}'")
            check_col(rt, rc, f"join '{spec}'")
            group.append(JoinEdge(lt, lc, rt, rc))
        if not group:
            raise ProfileError(f"join '{spec}' is not of the form T1.C1 = T2.C2")
        joins.append(group)
    synonyms = {str(k).lower(): str(v).upper() for k, v in (raw.get("synonyms") or {}).items()}
    for term, target in synonyms.items():
        if "." in target:
            check_col(*target.split(".", 1), where=f"synonym '{term}'")
        elif target not in tables:
            raise ProfileError(f"synonym '{term}': unknown table {target}")
    metrics = {
        str(k): Metric(str(k), str(v["sql"]), str(v.get("desc", "")), [str(s) for s in v.get("synonyms", [])], v.get("format"))
        for k, v in (raw.get("metrics") or {}).items()
    }
    default_paths = [[str(t).upper() for t in path] for path in raw.get("default_paths") or []]
    for path in default_paths:
        for a, b in zip(path, path[1:]):
            if not any({e.left_table, e.right_table} == {a, b} for g in joins for e in g):
                raise ProfileError(f"default path {path}: {a} and {b} are not directly joined")
    sf = raw.get("snowflake") or {}
    anchor = raw.get("time_anchor")
    if anchor:
        check_col(*str(anchor).upper().split(".", 1), where="time_anchor")
    profile = Profile(
        name=str(raw.get("name", name)),
        title=str(raw.get("title", name)),
        description=str(raw.get("description", "")),
        database=str(sf.get("database", "DATAMIND")).upper(),
        schema=str(sf.get("schema", name)).upper(),
        cloud_allowed=bool(raw.get("cloud_allowed", False)),
        max_rows=int(raw.get("max_rows", 1000)),
        time_anchor=str(anchor).upper() if anchor else None,
        tables=tables,
        joins=joins,
        metrics=metrics,
        synonyms=synonyms,
        rules=[str(r) for r in raw.get("rules") or []],
        default_paths=default_paths,
        examples=[e for e in raw.get("examples") or [] if isinstance(e, dict) and "question" in e and "plan" in e],
    )
    for e in profile.edges():
        profile._union((e.left_table, e.left_col), (e.right_table, e.right_col))
    _merge_cache(profile, (cache_dir or CACHE_DIR) / f"{profile.name}.json")
    return profile


def _merge_cache(profile: Profile, path: Path) -> None:
    """Fill auto values and date coverage from the introspection cache, if it exists."""
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    for key, values in (data.get("values") or {}).items():
        t, c = key.upper().split(".", 1)
        col = profile.tables.get(t, Table(t, "", {})).columns.get(c)
        if col is not None and col.auto_values:
            col.values = [str(v) for v in values]
    for key, (lo, hi) in (data.get("coverage") or {}).items():
        profile.coverage[key.upper()] = (str(lo), str(hi))
