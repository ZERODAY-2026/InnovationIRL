"""How this answer was made: five steps, each with a one-line outcome and its duration from the Envelope."""
from __future__ import annotations

from html import escape

import streamlit as st

from app.ui.common import flag, html, kv, seconds
from datamind.contracts import Envelope, LLMCall


def tier_text(call: LLMCall) -> str:
    text = f"{call.tier} / {call.model}"
    if call.fallback_used:
        text += " (backup tier" + (f"; {call.note}" if call.note else "") + ")"
    return text


def _short(text: str, n: int = 70) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def _items(values: list[str]) -> str:
    return " · ".join(f"<code>{escape(v)}</code>" for v in values)


def render(dm, env: Envelope) -> None:
    plan, gate, x, r, t = env.plan, env.gate, env.execution, env.receipt, env.timings_ms
    if plan is None or x is None:
        return
    plans = [c for c in env.llm_calls if c.role == "plan"]
    explains = [c for c in env.llm_calls if c.role == "explain"]
    repairs = max(len(plans) - 1, 0)
    fq = f"{dm.profile.database}.{dm.profile.schema}"
    rerun = env.previous_query_id is not None
    st.markdown("### How this answer was made")
    with st.container(key="trace"):
        with st.expander(f"1. Understood: {_short(plan.standalone_question)} · timed with step 2"):
            hints = dm.profile.value_hints(env.question)
            matched = "<br>".join(escape(h) for h in hints)
            if not matched and r and r.filters:
                matched = _items(r.filters) + " (literal values used in the filters)"
            html(kv([("Question as asked", escape(env.question)),
                     ("Read as (standalone question)", escape(plan.standalone_question)),
                     ("Assumptions", "<br>".join(escape(a) for a in plan.assumptions) or "None stated"),
                     ("Matched values", matched or "No value matching was needed")]))
        tier = tier_text(plans[-1]) if plans else "not called (re-run of the saved SQL)"
        with st.expander(f"2. Planned: one SELECT over {len(r.tables) if r else 0} table(s), written by Gemma · "
                         f"{seconds(t.get('plan_ms'))}", expanded=True):
            st.code(x.sql, language="sql")
            if r:
                html(kv([("Tables", _items(r.tables)), ("Joins", _items(r.joins)), ("Filters", _items(r.filters)),
                         ("Grouped by", _items(r.group_by)), ("Aggregations", _items(r.measures)),
                         ("Sorted by", _items(r.order_by)), ("Row limit", f"{r.limit} rows" if r.limit else ""),
                         ("Planned by", escape(tier))]))
        outcome = "read-only, allow-listed, EXPLAIN passed" + (f", {repairs} repair(s)" if repairs else "")
        check_ms = None if rerun else (t.get("gate_ms") or 0) + (t.get("explain_ms") or 0)
        with st.expander(f"3. Checked: {outcome} · {seconds(check_ms)}"):
            html(kv([("Read-only SELECT", flag(True, "Passed: a single SELECT, no writes or DDL")),
                     ("Tables allow-listed", flag(True, "Passed") + " " + _items(gate.tables if gate else [])),
                     ("Join paths", (_items(r.joins) + " (declared in the profile)") if r and r.joins else "No joins"),
                     ("EXPLAIN in Snowflake", flag(True, "Passed: the plan compiled before the query ran")),
                     ("Repairs", f"{repairs} (the gate or Snowflake sent the SQL back to Gemma)" if repairs else "0"),
                     ("Row cap", escape(str(gate.row_cap)) if gate and gate.row_cap else ""),
                     ("Warnings", "<br>".join(flag(None, w) for w in (gate.warnings if gate else [])))]))
        with st.expander(f"4. Run in Snowflake: {x.row_count} row(s) in {x.elapsed_ms / 1000:.1f} s · "
                         f"{seconds(t.get('execute_ms'))}"):
            html(kv([("Query ID", f"<code>{escape(x.query_id)}</code>"),
                     ("Rows returned", f"{x.row_count}" + (" (display capped)" if x.truncated else "")),
                     ("Elapsed in Snowflake", f"{x.elapsed_ms / 1000:.2f} s"),
                     ("Tables read", _items([f"{fq}.{name}" for name in (r.tables if r else [])])),
                     ("Ran at", escape(x.executed_at)),
                     ("Role / warehouse", escape(f"{x.role or '?'} / {x.warehouse or '?'}"))]))
        a = env.answer
        pointers = [s for s in a.segments if s.row is not None] if a else []
        how = ("deterministic answer, the model text failed the checks" if a and a.fallback_used
               else f"Gemma wording, {len(pointers)} value(s) taken from result cells")
        with st.expander(f"5. Explained: {how} · {seconds(t.get('explain_answer_ms'))}"):
            cells = "<br>".join(f"<b>{escape(s.text)}</b> ← row {s.row}, column <code>{escape(s.column or '')}</code>"
                                for s in pointers)
            html(kv([("Values and their cells", cells or "No cell values in the text"),
                     ("Fallback used", "Yes: code wrote the answer from the rows" if a and a.fallback_used else "No"),
                     ("Rejected numbers", escape(", ".join(a.rejected_numbers)) if a and a.rejected_numbers else ""),
                     ("Worded by", escape(tier_text(explains[-1])) if explains else "code (no model call)")]))
