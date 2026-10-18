"""Ask tab: question box, answer, result table, trace and every non-answer state."""
from __future__ import annotations

import inspect
from html import escape

import streamlit as st

from app.ui import charts, extras, stepper, trace
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


def _pick() -> None:
    choice = st.session_state.get("example_pick")
    if choice:
        _queue(choice)
    st.session_state.example_pick = None


def chips_html(dm, env: Envelope | None, catalog_ok: bool) -> str:
    s = dm.settings
    calls = [c for c in (env.llm_calls if env else []) if c.role == "plan"]
    if calls:
        gemma = f"Gemma {calls[-1].tier} · {calls[-1].model}" + (" · backup tier" if calls[-1].fallback_used else "")
    else:
        gemma = f"Gemma {s.gemma_provider} · {s.gemini_model_for('plan')}"
    role = getattr(dm.runner, "role", None)
    if role or catalog_ok:
        sf, cls = "Snowflake connected" + (f" · {role}" if role else ""), "ok"
    else:
        sf, cls = ("Snowflake configured, not connected" if s.snowflake_ready else "Snowflake not configured"), "bad"
    chips = [("", f"Dataset · {dm.profile.title}"), ("", gemma), (cls, sf)]
    return "<div class='dm-chips'>" + "".join(f"<span class='dm-chip {c}'>{escape(t)}</span>" for c, t in chips) + "</div>"


def _ask_live(dm, question: str) -> Envelope:
    """Run the pipeline and redraw the stepper after each real step (progress events from DataMind.ask)."""
    slot = st.empty()

    def draw(phase: str, env: Envelope) -> None:
        with slot.container():
            stepper.render_live(dm, env, phase)

    draw("start", Envelope(question=question, profile=dm.profile.name, status="ERROR"))
    if "progress" in inspect.signature(dm.ask).parameters:
        env = dm.ask(question, _context(), progress=draw)
    else:  # an engine cached before the progress callback existed: restart the app for live steps
        env = dm.ask(question, _context())
    slot.empty()
    return env


def render(dm, catalog_ok: bool) -> Envelope | None:
    st.session_state.setdefault("history", [])
    examples = [e["question"] for e in dm.profile.examples if e.get("question")][:4]
    with st.form("ask_form", border=False):
        box, button = st.columns([6, 1], vertical_alignment="bottom")
        box.text_area("Your question about the data", key="question", height=68, label_visibility="collapsed",
                      placeholder=(f"For example: {examples[0]}" if examples else "Ask about the data") + "  (Ctrl+Enter to ask)")
        asked = button.form_submit_button("Ask", type="primary", key="ask", width="stretch")
    if examples:
        st.pills("Example questions", examples, key="example_pick", on_change=_pick)
    typed = (st.session_state.get("question") or "").strip() if asked else ""
    question = st.session_state.pop("pending", None) or typed
    if question:
        st.session_state.history.append(_ask_live(dm, question))
    env = st.session_state.history[-1] if st.session_state.history else None
    if env is None:
        html("<p class='dm-sub'>No question asked yet. Type one above or pick an example. Each answer shows the SQL "
             "that ran, its checks, the result rows and the Snowflake query ID.</p>")
        return None
    show(dm, env)
    return env


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
        left, right = st.container(), (st.container() if chart is not None else None)
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
        html(stepper.stepper_html(env, "final"))
        options = env.plan.options[:3] if env.plan else []
        cols = st.columns(max(len(options), 1))
        for i, option in enumerate(options):
            cols[i].button(option, key=f"clarify{i}", on_click=_queue, args=(option,))
    elif env.status in ("UNSUPPORTED", "UNSAFE"):
        lead = ("I can't answer that from this data." if env.status == "UNSUPPORTED"
                else "I won't run that: this assistant only reads data.")
        reason = env.message or (env.plan.reason if env.plan else None) or "not given"
        html(f"<div class='dm-answer'>{escape(lead)}</div>")
        html(stepper.stepper_html(env, "final"))
        html(f"<div class='dm-note'><b>Nothing was executed in Snowflake.</b><br>Blocked request: "
             f"{escape(env.question)}<br>Reason: {escape(reason)}</div>")
    else:
        html("<div class='dm-answer'>No answer this time.</div>")
        html(f"<div class='dm-note'><b>No result came back from Snowflake.</b><br>{escape(env.message or 'Unknown error.')}</div>")
        html(stepper.stepper_html(env, "final"))
        if env.gate and env.gate.errors:
            with st.expander("What the gate blocked (this SQL was not run)"):
                html("<br>".join(escape(e) for e in env.gate.errors))
                if env.plan and env.plan.sql:
                    st.code(env.plan.sql, language="sql")
