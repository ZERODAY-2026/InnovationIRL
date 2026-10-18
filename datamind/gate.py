"""Deterministic SQL gate: only one read-only query over the profile's tables may reach Snowflake.

Layers: parse (exactly one statement) -> read-only node and function policy -> FROM/JOIN sources are
tables or subqueries -> table allow-list -> grounding (reads a table, not only constants) -> column
existence -> key joins follow declared relationships -> row cap. Snowflake EXPLAIN and the read-only
role are further layers outside this module.
"""
from __future__ import annotations

import logging
import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope

from .contracts import GateReport
from .profile import Profile

logging.getLogger("sqlglot").setLevel(logging.ERROR)

MAX_SQL_CHARS = 20_000


def _classes(*names: str) -> tuple[type, ...]:
    return tuple(c for c in (getattr(exp, n, None) for n in names) if isinstance(c, type))


QUERY_ROOTS = _classes("Select", "SetOperation", "Union", "Intersect", "Except")
BLOCKED_NODES = _classes(
    "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "Command", "Copy", "Set", "Use",
    "Grant", "Revoke", "TruncateTable", "Transaction", "Commit", "Rollback", "Into", "Describe", "Show",
    "Put", "Get", "Analyze", "Cache", "LoadData", "Pragma", "Kill", "Refresh", "Execute", "Lateral", "Unnest",
)
DENIED_FUNCTIONS = {
    "RESULT_SCAN", "GET_DDL", "GENERATOR", "LAST_QUERY_ID", "GET_PRESIGNED_URL", "BUILD_SCOPED_FILE_URL",
    "BUILD_STAGE_FILE_URL", "GET_STAGE_LOCATION", "GET_ABSOLUTE_PATH", "GET_RELATIVE_PATH", "CURRENT_ACCOUNT",
    "CURRENT_ACCOUNT_NAME", "CURRENT_ORGANIZATION_NAME", "CURRENT_IP_ADDRESS", "CURRENT_USER", "CURRENT_SESSION",
}
_SYSTEM_FN = re.compile(r"(?i)\bsystem\$")


def check_sql(sql: str | None, profile: Profile, max_rows: int | None = None) -> GateReport:
    text = (sql or "").strip()
    if not text:
        return _fail("the query is empty")
    if len(text) > MAX_SQL_CHARS:
        return _fail(f"the query is longer than {MAX_SQL_CHARS} characters")
    if _SYSTEM_FN.search(text):
        return _fail("SYSTEM$ functions are not allowed")
    try:
        statements = [s for s in sqlglot.parse(text, read="snowflake") if s is not None]
    except ParseError as exc:
        return _fail(f"the query could not be parsed: {_first_line(exc)}")
    if len(statements) != 1:
        return _fail(f"exactly one statement is allowed, found {len(statements)}")
    tree = statements[0]
    if not isinstance(tree, QUERY_ROOTS):
        return _fail(f"only SELECT queries are allowed, got {type(tree).__name__.upper()}")

    errors: list[str] = []
    for node in tree.walk():
        if isinstance(node, BLOCKED_NODES):
            errors.append(f"{type(node).__name__.upper()} is not allowed in a read-only query")
        elif isinstance(node, exp.Func):
            name = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).upper()
            if name in DENIED_FUNCTIONS or name.startswith("SYSTEM$"):
                errors.append(f"function {name} is not allowed")
    for select in tree.find_all(exp.Select):
        if any(isinstance(p, exp.Star) or (isinstance(p, exp.Column) and isinstance(p.this, exp.Star))
               for p in select.expressions):
            errors.append("SELECT * is not allowed; list the columns the answer needs")
            break
    for clause in [*tree.find_all(exp.From), *tree.find_all(exp.Join)]:
        source = clause.this
        if not isinstance(source, (exp.Table, exp.Subquery)):
            errors.append("only tables and subqueries may appear in FROM and JOIN")

    cte_names = {cte.alias_or_name.upper() for cte in tree.find_all(exp.CTE)}
    tables_used: set[str] = set()
    for table in tree.find_all(exp.Table):
        name = (table.name or "").upper()
        if not name:
            errors.append("unnamed table sources are not allowed")
            continue
        if name in cte_names and not table.args.get("db"):
            continue
        if name not in profile.tables:
            errors.append(f"table {table.sql(dialect='snowflake')} is not part of the '{profile.name}' dataset")
            continue
        db, catalog = table.text("db").upper(), table.text("catalog").upper()
        if db and db != profile.schema:
            errors.append(f"table {table.sql(dialect='snowflake')} must be in schema {profile.schema}")
        if catalog and catalog != profile.database:
            errors.append(f"table {table.sql(dialect='snowflake')} must be in database {profile.database}")
        tables_used.add(name)
    if not errors and not tables_used:
        errors.append("the query must read from at least one dataset table")
    if not errors and _only_constants(tree):
        errors.append("the query only selects constants; answers must come from the data")
    if errors:
        return _fail(*errors, tables=tables_used)

    warnings: list[str] = []
    qualified = _qualified(tree, profile, errors, warnings)
    if qualified is not None:
        errors.extend(_join_errors(qualified, profile))
    if errors:
        return _fail(*errors, tables=tables_used)

    cap = max_rows or profile.max_rows
    final = tree.copy()
    _apply_row_cap(final, cap + 1, warnings)
    return GateReport(
        ok=True,
        sql=final.sql(dialect="snowflake", pretty=True, comments=False),
        warnings=warnings,
        tables=sorted(tables_used),
        row_cap=cap,
    )


def _fail(*errors: str, tables: set[str] | None = None) -> GateReport:
    unique = list(dict.fromkeys(errors))
    return GateReport(ok=False, errors=unique, tables=sorted(tables or []))


def _first_line(exc: Exception) -> str:
    return str(exc).strip().splitlines()[0][:200] if str(exc).strip() else type(exc).__name__


def _only_constants(tree: exp.Expression) -> bool:
    """True if every projection of every top-level SELECT is a constant (no column, star, aggregate or window)."""
    selects = [tree] if isinstance(tree, exp.Select) else [s for s in tree.find_all(exp.Select) if s.parent_select is None]
    for select in selects:
        for projection in select.expressions:
            if isinstance(projection, exp.Star) or projection.find(exp.Column, exp.Star, exp.AggFunc, exp.Window, exp.Subquery):
                return False
    return True


def _qualified(tree: exp.Expression, profile: Profile, errors: list[str], warnings: list[str]) -> exp.Expression | None:
    stripped = tree.copy()
    for table in stripped.find_all(exp.Table):
        if (table.name or "").upper() in profile.tables:
            table.set("db", None)
            table.set("catalog", None)
    try:
        return qualify(
            stripped,
            schema=profile.schema_mapping(),
            dialect="snowflake",
            validate_qualify_columns=True,
            identify=False,
        )
    except OptimizeError as exc:
        errors.append(f"unknown or ambiguous column: {_first_line(exc)}")
    except Exception as exc:  # sqlglot cannot model every Snowflake construct; EXPLAIN still checks it
        warnings.append(f"column check skipped ({type(exc).__name__}); Snowflake EXPLAIN will validate instead")
    return None


def _join_errors(qualified: exp.Expression, profile: Profile) -> list[str]:
    """Equality between two key columns must link the same kind of key (declared join domains)."""
    errors: list[str] = []
    for scope in traverse_scope(qualified):
        alias_table = {
            alias.upper(): source.name.upper()
            for alias, source in scope.sources.items()
            if isinstance(source, exp.Table) and source.name.upper() in profile.tables
        }
        for eq in scope.expression.find_all(exp.EQ):
            if eq.find_ancestor(exp.Select) is not scope.expression:
                continue
            left, right = eq.left, eq.right
            if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
                continue
            lt, rt = alias_table.get(left.table.upper()), alias_table.get(right.table.upper())
            if not lt or not rt:
                continue
            lc, rc = left.name.upper(), right.name.upper()
            if profile.is_key(lt, lc) and profile.is_key(rt, rc) and not profile.same_key_domain(lt, lc, rt, rc):
                errors.append(f"join {lt}.{lc} = {rt}.{rc} does not match a declared relationship")
    if errors:
        errors.append("declared joins: " + "; ".join(str(e) for e in profile.edges()))
    return errors


def _apply_row_cap(tree: exp.Expression, cap: int, warnings: list[str]) -> None:
    if tree.args.get("fetch") is not None:
        return  # FETCH FIRST n ROWS already bounds the result; the executor also caps fetched rows
    limit = tree.args.get("limit")
    if limit is None:
        tree.set("limit", exp.Limit(expression=exp.Literal.number(cap)))
        return
    value = limit.expression
    if isinstance(value, exp.Literal) and value.is_int and int(value.name) <= cap:
        return
    tree.set("limit", exp.Limit(expression=exp.Literal.number(cap)))
    warnings.append(f"row limit lowered to {cap - 1} rows for display")
