"""Ask tab: question box, answer, result table, trace and every non-answer state."""
from __future__ import annotations

from html import escape

import streamlit as st

from app.ui import charts, extras, trace
from app.ui.common import answer_html, html, split_note, table_html
from datamind.contracts import Envelope
from datamind.prompts import Turn


def _queue(text: str) -> None:
    st.session_state.question = text
    st.session_state.pending = text


def _context() -> list[Turn]:
    """Previous answered questions, so follow-ups like "and in 1994?" resolve."""
    turns = []
    for env in st.session_state.history[-3:]:
        if env.status == "ANSWER" and env.execution and env.plan:
            turns.append(Turn(env.plan.standalone_question or env.question, env.execution.sql,
                              env.execution.columns, env.execution.row_count))
    return turns


def status_line(dm, env: Envelope | None, catalog_ok: bool) -> str:
    s = dm.settings
    calls = [c for c in (env.llm_calls if env else []) if c.role == "plan"]
    gemma = trace.tier_text(calls[-1]) if calls else f"{s.gemma_provider} / {s.gemini_model_for('plan')} (configured)"
    role = getattr(dm.runner, "role", None)
    if role:
        sf = f"connected (role {role}, warehouse {dm.runner.warehouse})"
    elif catalog_ok:
        sf = "connected"
    else:
        sf = "configured, not yet connected" if s.snowflake_ready else "not configured"
    return f"Dataset: {dm.profile.title} · Gemma: {gemma} · Snowflake: {sf}"


def render(dm, catalog_ok: bool) -> None:
    st.session_state.setdefault("history", [])
    examples = [e["question"] for e in dm.profile.examples if e.get("question")][:4]
    st.text_area("Your question about the data", key="question", height=96,
                 placeholder=f"For example: {examples[0]}" if examples else "")
    asked = st.button("Ask", type="primary", key="ask")
    if examples:
        html("<p class='dm-small'>Example questions from this dataset's profile:</p>")
        for i, text in enumerate(examples):
            st.button(text, key=f"ex{i}", type="tertiary", on_click=_queue, args=(text,))
    typed = (st.session_state.get("question") or "").strip() if asked else ""
    question = st.session_state.pop("pending", None) or typed
    if question:
        with st.spinner("Gemma is writing the SQL; the gate and EXPLAIN check it; Snowflake runs it."):
            st.session_state.history.append(dm.ask(question, _context()))
    env = st.session_state.history[-1] if st.session_state.history else None
    html(f"<p class='dm-status'>{escape(status_line(dm, env, catalog_ok))}</p>")
    if env is None:
        html("<p class='dm-sub'>No question asked yet. Type one above or pick an example. Each answer shows the SQL "
             "that ran, its checks, the result rows and the Snowflake query ID.</p>")
        return
    show(dm, env)


def show(dm, env: Envelope) -> None:
    html(f"<p class='dm-q'>Question: {escape(env.question)}</p>")
    if env.status == "ANSWER" and env.execution and env.answer:
        body, note = split_note(env.answer.segments)
        html(f"<div class='dm-answer'>{answer_html(body)}</div>")
        if note:
            html(f"<div class='dm-note'>{escape(note)}</div>")
        if env.diagnostics:
            html("<div class='dm-note'><b>Why no rows matched</b> (checked in Snowflake, query "
                 f"<code>{escape(env.diagnostic_query_id or '?')}</code>):<br>"
                 + "<br>".join(escape(d) for d in env.diagnostics) + "</div>")
        fallback = [c for c in env.llm_calls if c.fallback_used]
        if fallback:
            html("<p class='dm-small'>The main Gemma tier was unavailable (for example a 429 rate limit); answered by "
                 f"the backup tier {escape(trace.tier_text(fallback[-1]))}.</p>")
        x = env.execution
        money = dm.profile.money_columns()
        chart = charts.build(x.columns, x.rows, money)
        left, right = st.columns([1, 1]) if chart is not None else (st.container(), None)
        with left:
            st.markdown("### Result rows")
            shown = "; first 25 shown" if len(x.rows) > 25 else ""
            html(f"<p class='dm-small'>{x.row_count} row(s) from Snowflake query <code>{escape(x.query_id)}</code>{shown}.</p>")
            html(table_html(x.columns, x.rows, money))
            if len(x.rows) > 25:
                with st.expander(f"All {len(x.rows)} returned rows"):
                    st.dataframe([dict(zip(x.columns, row)) for row in x.rows], width="stretch")
        if right is not None:
            with right:
                st.markdown("### Chart of the result rows")
                st.altair_chart(chart, theme=None, width="stretch")
        trace.render(dm, env)
        extras.actions(dm, env)
    elif env.status == "CLARIFY":
        html(f"<div class='dm-answer'>{escape(env.message or 'Could you say a little more?')}</div>")
        html("<p class='dm-small'>Nothing was run yet. Pick a reply or type your own question above.</p>")
        options = env.plan.options[:3] if env.plan else []
        cols = st.columns(max(len(options), 1))
        for i, option in enumerate(options):
            cols[i].button(option, key=f"clarify{i}", on_click=_queue, args=(option,))
    elif env.status in ("UNSUPPORTED", "UNSAFE"):
        lead = ("I can't answer that from this data." if env.status == "UNSUPPORTED"
                else "I won't run that: this assistant only reads data.")
        reason = env.message or (env.plan.reason if env.plan else None) or "not given"
        html(f"<div class='dm-answer'>{escape(lead)}</div>")
        html(f"<div class='dm-note'><b>Nothing was executed in Snowflake.</b><br>Blocked request: "
             f"{escape(env.question)}<br>Reason: {escape(reason)}</div>")
    else:
        html("<div class='dm-answer'>No answer this time.</div>")
        html(f"<div class='dm-note'><b>No result came back from Snowflake.</b><br>{escape(env.message or 'Unknown error.')}</div>")
        if env.gate and env.gate.errors:
            with st.expander("What the gate blocked (this SQL was not run)"):
                html("<br>".join(escape(e) for e in env.gate.errors))
                if env.plan and env.plan.sql:
                    st.code(env.plan.sql, language="sql")
