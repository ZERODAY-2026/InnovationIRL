"""Snowflake access for the app: key-pair login as a read-only role, query tag, server-side timeout.

Only SQL that passed datamind.gate reaches execute(); the read-only role is the second line of defence.
"""
from __future__ import annotations

import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import snowflake.connector
from snowflake.connector.errors import Error as SnowflakeError

from .config import Settings
from .contracts import ExecutionResult

STATEMENT_TIMEOUT_S = 60


class SnowflakeUnavailable(RuntimeError):
    """Snowflake is not configured or the login failed."""


class QueryError(RuntimeError):
    """Snowflake rejected or failed a query; carries the query ID when there is one."""

    def __init__(self, message: str, query_id: str | None = None):
        super().__init__(message)
        self.query_id = query_id


def _message(exc: Exception) -> str:
    return " ".join(str(getattr(exc, "msg", None) or exc).split())[:400]


class SnowflakeRunner:
    def __init__(self, settings: Settings, schema: str, tag: str | None = None):
        self.settings = settings
        self.schema = schema
        self.tag = re.sub(r"[^a-z0-9:_-]", "", (tag or f"datamind:{schema}:{uuid.uuid4().hex[:8]}").lower())
        self.role: str | None = None
        self.warehouse: str | None = None
        self._conn: Any = None

    def connect(self) -> Any:
        if self._conn is not None:
            return self._conn
        s = self.settings
        if not s.snowflake_ready:
            raise SnowflakeUnavailable(
                "Snowflake is not configured: set SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER and SNOWFLAKE_PRIVATE_KEY_PATH in .env"
            )
        params: dict[str, Any] = dict(
            account=s.snowflake_account,
            user=s.snowflake_user,
            authenticator="SNOWFLAKE_JWT",
            private_key_file=s.snowflake_private_key_path,
            role=s.snowflake_role,
            warehouse=s.snowflake_warehouse,
            database=s.snowflake_database,
            schema=self.schema,
            application="DataMind",
            login_timeout=30,
            enable_server_session_keep_alive_auto_detection=False,  # no async queries: always log out on close
        )
        if s.snowflake_private_key_passphrase:
            params["private_key_password"] = s.snowflake_private_key_passphrase
        try:
            self._conn = snowflake.connector.connect(**params)
            cur = self._conn.cursor()
            cur.execute(f"ALTER SESSION SET QUERY_TAG = '{self.tag}', STATEMENT_TIMEOUT_IN_SECONDS = {STATEMENT_TIMEOUT_S}")
            cur.execute("SELECT CURRENT_ROLE(), CURRENT_WAREHOUSE()")
            self.role, self.warehouse = cur.fetchone()
        except SnowflakeError as exc:
            self._conn = None
            raise SnowflakeUnavailable(f"Snowflake login failed: {_message(exc)}") from exc
        return self._conn

    def explain(self, sql: str) -> list[str]:
        """Compile without running. Returns plan problems (cartesian join); raises QueryError on compile errors."""
        cur = self.connect().cursor()
        try:
            cur.execute("EXPLAIN USING TABULAR " + sql)
        except SnowflakeError as exc:
            raise QueryError(_message(exc), getattr(exc, "sfqid", None)) from exc
        columns = [_col_name(d) for d in cur.description]
        if cartesian_scan(columns, cur.fetchall()):
            return ["the query plan cross-joins table rows (a join condition is probably missing)"]
        return []

    def execute(self, sql: str, max_rows: int) -> ExecutionResult:
        cur = self.connect().cursor()
        started = datetime.now(timezone.utc)
        t0 = time.perf_counter()
        try:
            cur.execute(sql)
        except SnowflakeError as exc:
            raise QueryError(_message(exc), getattr(exc, "sfqid", None)) from exc
        query_id = cur.sfqid
        columns = [_col_name(d) for d in cur.description]
        fetched = cur.fetchmany(max_rows + 1)
        elapsed = int((time.perf_counter() - t0) * 1000)
        rows = [list(r) for r in fetched[:max_rows]]
        return ExecutionResult(
            query_id=query_id,
            sql=sql,
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=len(fetched) > max_rows,
            elapsed_ms=elapsed,
            executed_at=started.isoformat(timespec="seconds"),
            role=self.role,
            warehouse=self.warehouse,
        )

    def run_unchecked(self, sql: str) -> tuple[str | None, list[tuple[Any, ...]]]:
        """For setup, smoke tests and evidence export only (never for model SQL)."""
        query_id, _, rows = self.run_trusted(sql)
        return query_id, rows

    def run_trusted(self, sql: str) -> tuple[str | None, list[str], list[tuple[Any, ...]]]:
        """Hand-written SQL (gold answers, introspection): returns query ID, column names and rows."""
        cur = self.connect().cursor()
        try:
            cur.execute(sql)
        except SnowflakeError as exc:
            raise QueryError(_message(exc), getattr(exc, "sfqid", None)) from exc
        columns = [_col_name(d) for d in cur.description] if cur.description else []
        return cur.sfqid, columns, cur.fetchall()

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None


def cartesian_scan(columns: list[str], rows: list[Any]) -> bool:
    """True if an EXPLAIN plan has a CartesianJoin fed directly by table scans.

    Cross-joining single-row aggregates (e.g. revenue 1997 minus revenue 1996) is legitimate and not flagged:
    the walk down from the join stops at Aggregate and WithReference nodes.
    """
    names = [c.lower() for c in columns]
    if "operation" not in names:
        return False
    op = names.index("operation")
    if not any(str(r[op]).lower() == "cartesianjoin" for r in rows):
        return False
    parent_name = next((n for n in ("parentoperators", "parent") if n in names), None)
    if "id" not in names or parent_name is None:
        return True  # cannot tell what feeds the join: stay conservative
    ident, parent = names.index("id"), names.index(parent_name)
    step = names.index("step") if "step" in names else None
    children: dict[tuple, list[Any]] = {}
    for r in rows:
        for p in _parents(r[parent]):  # Snowflake lists parents as "[4, 13]" (a CTE can feed several operators)
            children.setdefault((r[step] if step is not None else 0, p), []).append(r)

    def scans_below(r: Any) -> bool:
        for child in children.get((r[step] if step is not None else 0, r[ident]), []):
            kind = str(child[op]).lower()
            if kind == "tablescan":
                return True
            if kind not in ("aggregate", "withreference") and scans_below(child):
                return True
        return False

    return any(scans_below(r) for r in rows if str(r[op]).lower() == "cartesianjoin")


def _parents(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, int):
        return [value]
    return [int(x) for x in re.findall(r"\d+", str(value))]


def _col_name(description_item: Any) -> str:
    return str(getattr(description_item, "name", None) or description_item[0])
