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

st.title("DataMind")
html("<p class='dm-sub'>Ask a question in plain English. Snowflake computes the answer; Gemma plans the SQL and words "
     "the result. Every answer shows the SQL that ran, its checks, the result rows and the Snowflake query ID.</p>")
catalog, error = None, None
try:
    with st.spinner("Reading the table list, row counts and samples from Snowflake."):
        catalog = data_tab.load_catalog(dm.profile.name, type(dm).__name__, dm)
except Exception as exc:
    error = str(exc)

ask, data = st.tabs(["Ask", "Data"])
with ask:
    ask_tab.render(dm, catalog is not None)
with data:
    data_tab.render(dm, catalog, error)
