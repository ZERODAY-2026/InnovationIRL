"""Small formatting helpers. They format Snowflake values; they never compute statistics."""
from __future__ import annotations

from decimal import Decimal
from html import escape
from typing import Any

import streamlit as st

from datamind.contracts import Segment
from datamind.render import format_value

NOTE_PREFIX = "Note: the data only covers"


def html(markup: str) -> None:
    st.markdown(markup, unsafe_allow_html=True)


def seconds(ms: int | None) -> str:
    return "no separate timing" if ms is None else f"{ms / 1000:.1f} s"


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def split_note(segments: list[Segment]) -> tuple[list[Segment], str | None]:
    """The pipeline appends the out-of-coverage note to the answer; show it on its own line instead."""
    body = [s for s in segments if not s.text.strip().startswith(NOTE_PREFIX)]
    notes = [s.text.strip() for s in segments if s.text.strip().startswith(NOTE_PREFIX)]
    return body, (notes[0] if notes else None)


def answer_html(segments: list[Segment]) -> str:
    return "".join(f"<b>{escape(s.text)}</b>" if s.row is not None else escape(s.text) for s in segments)


def table_html(columns: list[str], rows: list[list[Any]], money: set[str], limit: int = 25) -> str:
    head = "".join(f"<th class='num'>{escape(c)}</th>" if rows and is_number(rows[0][i]) else f"<th>{escape(c)}</th>"
                   for i, c in enumerate(columns))
    body = "".join("<tr>" + "".join(f"<td class='{'num' if is_number(v) else ''}'>{escape(format_value(v, c, money))}</td>"
                                    for c, v in zip(columns, row)) + "</tr>" for row in rows[:limit])
    return f"<table class='dm-table'><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def kv(items: list[tuple[str, str]]) -> str:
    """A definition list; values are already escaped HTML."""
    return "<dl class='dm-kv'>" + "".join(f"<dt>{escape(k)}</dt><dd>{v}</dd>" for k, v in items if v) + "</dl>"


def flag(ok: bool | None, text: str) -> str:
    cls = "dm-pass" if ok else ("dm-warnflag" if ok is None else "dm-fail")
    return f"<span class='{cls}'>{escape(text)}</span>"
