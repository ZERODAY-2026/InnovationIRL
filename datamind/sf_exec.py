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
        columns = [_col_name(d).lower() for d in cur.description]
        rows = cur.fetchall()
        if "operation" in columns:
            i = columns.index("operation")
            if any(str(r[i]).lower() == "cartesianjoin" for r in rows):
                return ["the query plan contains a cartesian join (a join condition is probably missing)"]
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
        cur = self.connect().cursor()
        try:
            cur.execute(sql)
        except SnowflakeError as exc:
            raise QueryError(_message(exc), getattr(exc, "sfqid", None)) from exc
        return cur.sfqid, cur.fetchall()

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None


def _col_name(description_item: Any) -> str:
    return str(getattr(description_item, "name", None) or description_item[0])
