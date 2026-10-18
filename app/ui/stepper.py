"""Five-node stepper: Understand, Plan, Check, Run in Snowflake, Explain.

While a question runs it is redrawn from DataMind.ask's progress events; afterwards from the final Envelope.
Every state, duration and revealed detail comes from the Envelope: nothing is simulated.
"""
from __future__ import annotations

import time
from html import escape

import streamlit as st

from app.ui.common import html, seconds
from datamind.contracts import Envelope

NAMES = ["Understand", "Plan", "Check", "Run in Snowflake", "Explain"]
WORDS = {"waiting": "Waiting", "active": "Running", "done": "Done", "failed": "Stopped", "skipped": "Skipped",
         "warn": "Note"}


def _short(text: str, n: int = 90) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def states(env: Envelope, phase: str) -> list[tuple[str, str]]:
    t = env.timings_ms
    plans = [c for c in env.llm_calls if c.role == "plan"]
    repairs = max(len(plans) - 1, 0)
    wait, skip = ("waiting", "Waiting"), ("skipped", "Skipped: nothing ran")
    understood = ("done", "Read by the same Gemma call as Plan")
    planned = ("done", f"SQL written in {seconds(t.get('plan_ms'))}" + (f", {repairs} repair(s)" if repairs else ""))
    checked = ("done", f"Passed in {seconds((t.get('gate_ms') or 0) + (t.get('explain_ms') or 0))}")
    x = env.execution
    if env.previous_query_id:  # a re-run replays saved SQL: no model call and no new plan
        planned, checked = ("done", "Saved SQL reused, no model call"), ("done", "Checked on the first run")
    if phase == "start":
        return [("active", "Gemma is reading the question"), ("active", "Gemma is writing the SQL"), wait, wait, wait]
    if phase == "repair":
        err = env.gate.errors[0] if env.gate and env.gate.errors else "a check failed"
        return [understood, ("active", f"Gemma is repairing the SQL (attempt {len(plans) + 1})"),
                ("failed", f"Sent back: {_short(err, 80)}"), wait, wait]
    if phase == "planned":
        return [understood, planned, ("active", "Checking read-only, allow-list, EXPLAIN"), wait, wait]
    if phase == "checked":
        return [understood, planned, checked, ("active", "Running in Snowflake"), wait]
    if phase == "ran" and x is not None:
        return [understood, planned, checked, ("done", f"{x.row_count} row(s) in {seconds(t.get('execute_ms'))}"),
                ("active", "Wording the answer from result cells")]
    reason = _short(env.message or (env.plan.reason if env.plan else None) or "no reason given")
    if env.status == "CLARIFY":
        return [("failed", f"Needs clarification: {reason}"), skip, skip, skip, skip]
    if env.status in ("UNSUPPORTED", "UNSAFE"):
        return [understood, ("failed", f"Refused ({env.status.lower()}): {reason}"), skip, skip, skip]
    if env.status == "ERROR" or x is None:
        if env.plan is None:
            return [("failed", reason), skip, skip, skip, skip]
        if (env.gate is not None and not env.gate.ok) or "explain_ms" not in t:
            return [understood, planned, ("failed", f"Stopped after {repairs} repair(s): {reason}"), skip, skip]
        return [understood, planned, checked, ("failed", reason), skip]
    explains = [c for c in env.llm_calls if c.role == "explain"]
    if x.row_count == 0:
        run = ("warn", f"0 rows matched in {seconds(t.get('execute_ms'))}")
        explain = ("done", "Explained why no rows matched" if env.diagnostics else "Reported that no rows matched")
    else:
        run = ("done", f"{x.row_count} row(s) in {seconds(t.get('execute_ms'))}")
        if env.answer and env.answer.fallback_used:
            explain = ("warn", "Code wrote the answer: the model text failed the checks")
        elif explains:
            explain = ("done", f"Worded in {seconds(t.get('explain_answer_ms'))}, values from cells")
        else:
            explain = ("done", "Written by code from the rows")
    return [understood, planned, checked, run, explain]


def stepper_html(env: Envelope, phase: str) -> str:
    items = states(env, phase)
    fresh = max([i for i, (s, _) in enumerate(items) if s in ("done", "warn")], default=-1)
    nodes = "".join(
        f"<li class='dm-node {state}{' fresh' if i == fresh else ''}'><span class='dm-dot' aria-hidden='true'></span>"
        f"<span class='dm-node-name'>{i + 1}. {escape(name)}</span>"
        f"<span class='dm-node-text'><span class='dm-sr'>{WORDS[state]}: </span>{escape(text)}</span></li>"
        for i, (name, (state, text)) in enumerate(zip(NAMES, items)))
    return f"<div class='dm-flow'><ol class='dm-stepper' aria-label='How the query happens'>{nodes}</ol></div>"


def render_live(dm, env: Envelope, phase: str) -> None:
    """The stepper plus what each finished step actually produced (shown while the question runs)."""
    html(stepper_html(env, phase))
    plan = env.plan
    if plan is None or phase == "start":
        return
    hints = dm.profile.value_hints(env.question)
    html(f"<p class='dm-reveal'><b>Understood:</b> {escape(plan.standalone_question)}"
         + (f"<br><b>Matched values:</b> {escape('; '.join(hints))}" if hints else "") + "</p>")
    sql = env.gate.sql if env.gate and env.gate.ok and env.gate.sql else plan.sql
    if sql:
        html("<p class='dm-reveal'><b>SQL</b> " + ("being checked" if phase == "planned" else "sent back to Gemma"
                                                     if phase == "repair" else "that runs") + ":</p>")
        if phase == "planned":  # reveal the real SQL line by line, at most 1.2 s in total
            code, lines = st.empty(), sql.splitlines()
            for k in range(1, len(lines)):
                code.code("\n".join(lines[:k]), language="sql")
                time.sleep(min(0.12, 1.2 / len(lines)))
            code.code(sql, language="sql")
        else:
            st.code(sql, language="sql")
    if phase == "repair" and env.gate:
        html("<p class='dm-reveal'><b>Why it was sent back:</b> " + escape("; ".join(env.gate.errors[:2])) + "</p>")
    if phase in ("checked", "ran") and env.gate:
        html("<p class='dm-reveal'><b>Checked:</b> one read-only SELECT, tables allow-listed ("
             + escape(", ".join(env.gate.tables)) + "), EXPLAIN compiled in Snowflake.</p>")
    if phase == "ran" and env.execution:
        x = env.execution
        tables = ", ".join(f"{dm.profile.database}.{dm.profile.schema}.{n}" for n in (env.receipt.tables if env.receipt else []))
        html(f"<p class='dm-reveal'><b>Ran in Snowflake:</b> query <code>{escape(x.query_id)}</code>, {x.row_count} row(s), "
             f"read {escape(tables)}.</p>")
