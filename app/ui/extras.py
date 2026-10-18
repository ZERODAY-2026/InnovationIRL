"""Run again on current data (diff with both query IDs) and Dig deeper (Verified Insights)."""
from __future__ import annotations

from html import escape

import streamlit as st

from app.ui import charts
from app.ui.common import answer_html, html, seconds, table_html
from datamind.contracts import Envelope, InsightsReport
from datamind.insights import dig_deeper


def actions(dm, env: Envelope) -> None:
    qid = env.execution.query_id
    left, right = st.columns(2)
    if left.button("Run again on current data", key=f"rerun-{qid}", help="Runs the same SQL in Snowflake again; no new plan"):
        with st.spinner("Running the same SQL in Snowflake again."):
            st.session_state.setdefault("reruns", {})[qid] = dm.rerun(env)
    if right.button("Dig deeper", key=f"dig-{qid}",
                    help="Up to 3 follow-up analyses; only findings backed by their own Snowflake query are shown"):
        with st.spinner("Gemma proposes follow-up SQL; each query is checked and run in Snowflake."):
            st.session_state.setdefault("insights", {})[qid] = dig_deeper(dm, env)
    rerun = st.session_state.get("reruns", {}).get(qid)
    if rerun is not None:
        show_rerun(env, rerun)
    report = st.session_state.get("insights", {}).get(qid)
    if report is not None:
        show_insights(dm, report)


def show_rerun(env: Envelope, rerun: Envelope) -> None:
    st.markdown("### Run again on current data")
    if rerun.status == "ERROR" or rerun.execution is None:
        html(f"<div class='dm-note'><b>The re-run did not complete.</b><br>{escape(rerun.message or '')}</div>")
        return
    html(f"<p>First run: query <code>{escape(env.execution.query_id)}</code>. Re-run: query "
         f"<code>{escape(rerun.execution.query_id)}</code>, {rerun.execution.row_count} row(s), "
         f"{seconds(rerun.timings_ms.get('execute_ms'))}. Same SQL, no new plan.</p>")
    html("<ul>" + "".join(f"<li>{escape(c)}</li>" for c in rerun.changes) + "</ul>")
    if rerun.answer:
        html(f"<p><b>Answer on current data:</b> {answer_html(rerun.answer.segments)}</p>")


def show_insights(dm, report: InsightsReport) -> None:
    money = dm.profile.money_columns()
    st.markdown("### Verified insights")
    html("<p class='dm-small'>Each finding comes from its own read-only query, checked and run in Snowflake. "
         "Findings that could not be tied to result cells are dropped.</p>")
    for insight in report.shown:
        if insight.answer:
            html(f"<p>{answer_html(insight.answer.segments)}</p>")
        html(f"<p class='dm-small'>{escape(insight.title)} · Snowflake query <code>{escape(insight.query_id or '?')}</code></p>")
        chart = charts.build(insight.columns, insight.rows, money, height_per_bar=22)
        if chart is not None:
            st.altair_chart(chart, theme=None, width="stretch")
        with st.expander(f"SQL and rows for: {insight.title}"):
            st.code(insight.sql, language="sql")
            html(table_html(insight.columns, insight.rows, money))
    if not report.shown:
        html("<p>No finding passed verification this time.</p>")
    if report.dropped:
        html(f"<p class='dm-small'>{len(report.dropped)} proposed analysis(es) dropped: "
             + escape("; ".join(f"{d.title}: {d.reason}" for d in report.dropped)) + "</p>")
    if report.message:
        html(f"<p class='dm-small'>{escape(report.message)}</p>")
