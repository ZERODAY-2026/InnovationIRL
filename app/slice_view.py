"""Minimal review UI for the vertical slice: streamlit run app/slice_view.py

The full UI lives in app/ (UI owner); this page only shows the pipeline's evidence envelope.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datamind.config import load_settings  # noqa: E402
from datamind.contracts import Envelope  # noqa: E402
from datamind.pipeline import DataMind  # noqa: E402
from datamind.render import format_value  # noqa: E402

st.set_page_config(page_title="DataMind", layout="wide")


@st.cache_resource
def engine() -> DataMind:
    return DataMind(load_settings())


def md_escape(text: str) -> str:
    return "".join("\\" + ch if ch in "\\`*_{}[]()#+-.!|<>$" else ch for ch in text)


def show(env: Envelope, money: set[str]) -> None:
    if env.status == "ANSWER" and env.answer:
        st.markdown("".join(f"**{md_escape(s.text)}**" if s.row is not None or s.column == "row_count" else md_escape(s.text)
                            for s in env.answer.segments))
        kind = "deterministic answer (model text could not be verified)" if env.answer.fallback_used else "Gemma wording, values from result cells"
        qid = env.execution.query_id if env.execution else "?"
        st.caption(f"Computed in Snowflake (query ID {qid}) · {kind}")
        values = [s for s in env.answer.segments if s.row is not None]
        if values:
            st.caption("Values used: " + "; ".join(f"{s.text} ← row {s.row}, column {s.column}" for s in values))
    elif env.status == "CLARIFY":
        st.info(env.message or "Could you clarify?")
        for i, option in enumerate(env.plan.options if env.plan else []):
            if st.button(option, key=f"opt-{id(env)}-{i}"):
                st.session_state.pending = option
                st.rerun()
    elif env.status in ("UNSUPPORTED", "UNSAFE"):
        st.warning(env.message)
    else:
        st.error(env.message or "Something went wrong.")

    if env.plan and env.plan.assumptions:
        st.markdown("**Assumptions:** " + " · ".join(md_escape(a) for a in env.plan.assumptions))
    if env.receipt:
        r = env.receipt
        with st.expander("How this was computed", expanded=True):
            for label, items in (("Tables", r.tables), ("Joins", r.joins), ("Filters", r.filters),
                                 ("Grouped by", r.group_by), ("Measures", r.measures), ("Sorted by", r.order_by)):
                if items:
                    st.markdown(f"**{label}:** " + " · ".join(f"`{i}`" for i in items))
            if r.limit:
                st.markdown(f"**Limited to:** {r.limit} rows")
    if env.execution:
        x = env.execution
        with st.expander("Evidence: executed SQL and result rows"):
            st.code(x.sql, language="sql")
            st.text(f"Query ID {x.query_id} · role {x.role} · warehouse {x.warehouse} · {x.elapsed_ms} ms · "
                    f"{x.row_count} rows{' (display capped)' if x.truncated else ''} · ran at {x.executed_at}")
            frame = pd.DataFrame([[format_value(v, c, money) for c, v in zip(x.columns, row)] for row in x.rows],
                                 columns=x.columns)
            st.dataframe(frame, use_container_width=True)
    if env.llm_calls:
        st.caption(" · ".join(f"Gemma {c.role}: {c.tier}/{c.model} {c.latency_ms} ms{' cached' if c.cached else ''}"
                              f"{' (backup tier)' if c.fallback_used else ''}" for c in env.llm_calls))


st.title("DataMind: ask the data")
try:
    dm = engine()
except Exception as exc:  # configuration problems are shown, not raised
    st.error(f"Configuration problem: {exc}")
    st.stop()
st.caption(f"Dataset: {dm.profile.title}. Answers are computed in Snowflake; Gemma plans the query and words the answer.")
with st.sidebar:
    st.subheader("Runtime")
    st.json(dm.settings.public_summary())

st.session_state.setdefault("history", [])
question = st.chat_input("Ask a question about the data") or st.session_state.pop("pending", None)
if question:
    with st.spinner("Planning, checking and running the query..."):
        st.session_state.history.append(dm.ask(question))
for env in reversed(st.session_state.history):
    st.subheader(env.question)
    show(env, dm.profile.money_columns())
    st.divider()
