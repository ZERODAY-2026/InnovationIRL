"""DataMind dashboard: streamlit run app/main.py

Snowflake computes every number on this page; Python only formats Snowflake results.
app/slice_view.py stays as the minimal fallback view.
"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.ui import ask_tab, data_tab  # noqa: E402
from app.ui.common import html  # noqa: E402
from datamind.config import load_settings  # noqa: E402
from datamind.pipeline import DataMind  # noqa: E402

st.set_page_config(page_title="DataMind", layout="wide", initial_sidebar_state="collapsed")
html(f"<style>{(Path(__file__).parent / 'style.css').read_text(encoding='utf-8')}</style>")


@st.cache_resource
def engine() -> DataMind:
    return DataMind(load_settings())


try:
    dm = st.session_state["engine"] if "engine" in st.session_state else engine()  # tests inject a fake engine
except Exception as exc:  # configuration problems are shown, not raised
    st.error(f"Configuration problem: {exc}")
    st.stop()

html("<div class='dm-head'><h1 class='dm-wordmark'>DataMind</h1>"
     "<p class='dm-tagline'>Ask your Snowflake data in plain English and see the query behind every answer.</p></div>")
chips = st.empty()
with st.expander("How it works"):
    html("<p>Gemma reads the question and writes one read-only SQL query. A gate checks it (single SELECT, allow-listed "
         "tables, declared joins) and Snowflake compiles it with EXPLAIN before it runs. Snowflake computes every "
         "number. Gemma then words the answer, and each value in it is linked to a result cell; if that check fails, "
         "code writes the answer from the rows. Every answer shows the SQL that ran, its checks, the result rows and "
         "the Snowflake query ID.</p>")
catalog, error = None, None
try:
    with st.spinner("Reading the table list, row counts and samples from Snowflake."):
        catalog = data_tab.load_catalog(dm.profile.name, type(dm).__name__, dm)
except Exception as exc:
    error = str(exc)

ask, data = st.tabs(["Ask", "Data"])
chips.markdown(ask_tab.chips_html(dm, (st.session_state.get("history") or [None])[-1], catalog is not None),
               unsafe_allow_html=True)
with ask:
    latest = ask_tab.render(dm, catalog is not None)
chips.markdown(ask_tab.chips_html(dm, latest, catalog is not None), unsafe_allow_html=True)
with data:
    data_tab.render(dm, catalog, error)
