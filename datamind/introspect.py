"""Refresh profiles/cache/<profile>.json from Snowflake: date coverage and `values: auto` lists.

Run after setup and after data changes: python -m datamind.introspect [profile]
Uses a few read-only queries with identifiers taken from the profile YAML (validated), never model SQL.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone

from .config import load_settings
from .profile import CACHE_DIR, Profile, load_profile
from .sf_exec import QueryError, SnowflakeRunner, SnowflakeUnavailable

_IDENT = re.compile(r"^[A-Z_][A-Z0-9_$]*$")
MAX_VALUES = 500


def _ident(name: str) -> str:
    if not _IDENT.match(name):
        raise ValueError(f"unsafe identifier in profile: {name!r}")
    return name


def introspect(profile: Profile, runner) -> dict:
    data: dict = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "coverage": {}, "values": {}, "query_ids": []}
    prefix = f"{_ident(profile.database)}.{_ident(profile.schema)}"
    anchor = profile.time_anchor or ""
    for table in profile.tables.values():
        # date columns, plus a numeric time anchor such as CO2.YEAR
        dates = [c.name for c in table.columns.values()
                 if c.type in ("date", "timestamp") or f"{table.name}.{c.name}" == anchor]
        if dates:
            parts = ", ".join(f"MIN({_ident(c)}), MAX({_ident(c)})" for c in dates)
            qid, rows = runner.run_unchecked(f"SELECT {parts} FROM {prefix}.{_ident(table.name)}")
            data["query_ids"].append(qid)
            row = rows[0]
            for i, column in enumerate(dates):
                if row[2 * i] is not None:
                    data["coverage"][f"{table.name}.{column}"] = [str(row[2 * i]), str(row[2 * i + 1])]
        for column in table.columns.values():
            if column.auto_values:
                c = _ident(column.name)
                qid, rows = runner.run_unchecked(
                    f"SELECT DISTINCT {c} FROM {prefix}.{_ident(table.name)} WHERE {c} IS NOT NULL ORDER BY 1 LIMIT {MAX_VALUES}"
                )
                data["query_ids"].append(qid)
                data["values"][f"{table.name}.{column.name}"] = [str(r[0]) for r in rows]
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", nargs="?")
    args = parser.parse_args(argv)
    settings = load_settings()
    profile = load_profile(args.profile or settings.profile)
    runner = SnowflakeRunner(settings, profile.schema, tag=f"datamind:introspect:{profile.name}")
    try:
        data = introspect(profile, runner)
    except (SnowflakeUnavailable, QueryError) as exc:
        print(f"Introspection failed: {exc}")
        return 1
    finally:
        runner.close()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{profile.name}.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"Wrote {path.relative_to(CACHE_DIR.parent.parent)}: {len(data['coverage'])} date ranges, "
          f"{len(data['values'])} value lists, {len(data['query_ids'])} queries")
    for key, (lo, hi) in data["coverage"].items():
        print(f"  {key}: {lo} .. {hi}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
