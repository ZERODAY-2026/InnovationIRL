"""Export Snowflake's own record of DataMind's queries: evidence that answers ran in Snowflake at question time.

python -m datamind.history [--hours 24]
Reads INFORMATION_SCHEMA.QUERY_HISTORY_BY_USER for the app user, keeps queries tagged 'datamind:%', and writes
eval/results/query_history_<UTC>.csv (query IDs, times, status, rows, role, warehouse, first 160 characters of SQL).
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timezone

from .config import ROOT, load_settings
from .profile import load_profile
from .sf_exec import QueryError, SnowflakeRunner, SnowflakeUnavailable

HISTORY_SQL = """
SELECT QUERY_ID, START_TIME, QUERY_TAG, EXECUTION_STATUS, ROWS_PRODUCED, TOTAL_ELAPSED_TIME, ROLE_NAME,
       WAREHOUSE_NAME, LEFT(REGEXP_REPLACE(QUERY_TEXT, '\\\\s+', ' '), 160) AS QUERY_TEXT
FROM TABLE({database}.INFORMATION_SCHEMA.QUERY_HISTORY_BY_USER(
       END_TIME_RANGE_START => DATEADD('hour', -{hours}, CURRENT_TIMESTAMP()), RESULT_LIMIT => 10000))
WHERE QUERY_TAG LIKE 'datamind:%'
ORDER BY START_TIME
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, default=24)
    args = parser.parse_args(argv)
    settings = load_settings()
    profile = load_profile(settings.profile)
    runner = SnowflakeRunner(settings, profile.schema, tag="datamind:history")
    try:
        query_id, columns, rows = runner.run_trusted(
            HISTORY_SQL.format(database=profile.database, hours=max(1, min(args.hours, 168))))
    except (SnowflakeUnavailable, QueryError) as exc:
        print(f"History export failed: {exc}")
        return 1
    finally:
        runner.close()
    out_dir = ROOT / "eval" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"query_history_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.csv"
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        writer.writerows(rows)
    statuses: dict[str, int] = {}
    for row in rows:
        statuses[str(row[3])] = statuses.get(str(row[3]), 0) + 1
    print(f"{len(rows)} DataMind queries in the last {args.hours} h ({statuses}); history query {query_id}")
    print(f"wrote {out.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
