"""Data tab: where the data lives in Snowflake (tables, row counts, columns, samples, joins, coverage)."""
from __future__ import annotations

from html import escape

import streamlit as st

from app.ui.common import html, table_html


@st.cache_data(ttl=600, show_spinner=False)
def load_catalog(profile_name: str, engine_kind: str, _dm) -> dict:
    """Row counts and 5 sample rows per table, read with the trusted runner (visible columns only)."""
    p = _dm.profile
    fq = f"{p.database}.{p.schema}"
    union = " UNION ALL ".join(f"SELECT '{t.name}' AS TABLE_NAME, COUNT(*) AS ROW_COUNT FROM {fq}.{t.name}"
                               for t in p.tables.values())
    count_qid, _, rows = _dm.runner.run_trusted(union)
    samples = {}
    for t in p.tables.values():
        visible = [c.name for c in t.columns.values() if not c.hidden]
        qid, cols, sample = _dm.runner.run_trusted(f"SELECT {', '.join(visible)} FROM {fq}.{t.name} LIMIT 5")
        samples[t.name] = (qid, cols, [list(r) for r in sample])
    return {"count_query_id": count_qid, "counts": {r[0]: r[1] for r in rows}, "samples": samples}


def join_dot(profile) -> str:
    edges = []
    for group in profile.joins:
        label = " and ".join(f"{e.left_col} = {e.right_col}" for e in group)
        edges.append(f'"{group[0].left_table}" -- "{group[0].right_table}" [label="{label}"];')
    return ('graph { rankdir=LR; bgcolor="transparent"; '
            'node [shape=box, style=rounded, color="#5C584F", fontcolor="#1C1B19", fontname="Helvetica", fontsize=11]; '
            'edge [color="#8A8578", fontcolor="#5C584F", fontname="Helvetica", fontsize=9]; ' + " ".join(edges) + " }")


def render(dm, catalog: dict | None, error: str | None) -> None:
    p = dm.profile
    money = p.money_columns()
    st.markdown(f"### Where the data lives: Snowflake {p.database}.{p.schema}")
    html(f"<p class='dm-sub'>{escape(p.description)}</p>")
    if error:
        html(f"<div class='dm-note'><b>Could not read row counts and samples from Snowflake.</b><br>{escape(error)}</div>")
    elif catalog:
        html(f"<p class='dm-small'>Row counts from Snowflake query <code>{escape(str(catalog['count_query_id']))}</code>, "
             "cached for 10 minutes. Columns the app may not query are left out.</p>")
    for t in p.tables.values():
        visible = [c for c in t.columns.values() if not c.hidden]
        count = catalog["counts"].get(t.name) if catalog else None
        rows_text = f"{count:,} rows" if isinstance(count, int) else "row count unavailable"
        with st.expander(f"{p.database}.{p.schema}.{t.name} · {rows_text} · {len(visible)} columns"):
            html(f"<p class='dm-small'>{escape(t.description)}</p>")
            html(table_html(["Column", "Type", "Meaning"], [[c.name, c.type, c.desc] for c in visible], set(), limit=999))
            if catalog and t.name in catalog["samples"]:
                qid, cols, rows = catalog["samples"][t.name]
                html(f"<p class='dm-small'>Sample rows from Snowflake query <code>{escape(str(qid))}</code>.</p>")
                html(table_html(cols, rows, money))
    st.markdown("### How the tables join")
    html("<p class='dm-small'>The only join paths the gate allows, declared in the dataset profile.</p>")
    try:
        st.graphviz_chart(join_dot(p))
    except Exception:  # the diagram is a convenience; the list below carries the same facts
        pass
    with st.expander("Join paths as text"):
        html("<br>".join(f"<code>{escape(' and '.join(str(e) for e in group))}</code>" for group in p.joins))
    st.markdown("### Data coverage")
    if p.coverage:
        html("<br>".join(f"<code>{escape(k)}</code>: {escape(str(lo))} to {escape(str(hi))}"
                         for k, (lo, hi) in p.coverage.items()))
        html("<p class='dm-small'>Read from Snowflake when the profile was last introspected.</p>")
    else:
        html("<p class='dm-small'>Coverage dates have not been introspected for this dataset yet.</p>")
