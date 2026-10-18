"""At most one chart, chosen by code from the result's shape (never by the model). It plots returned rows only."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

import altair as alt
import pandas as pd

from app.ui.common import is_number
from datamind.render import format_value

ACCENT, HIGHLIGHT, INK, MUTED, RULE = "#0F766E", "#0B1F3A", "#0F172A", "#475569", "#CBD5E1"
TIME_HINTS = ("YEAR", "MONTH", "QUARTER", "WEEK", "DATE", "DAY")
ID_HINTS = ("KEY", "_ID")
MAX_BARS = 15


def _kind(column: str, values: list[Any]) -> str | None:
    present = [v for v in values if v is not None]
    name = column.upper()
    if not present:
        return None
    if all(isinstance(v, (date, datetime)) for v in present):
        return "date"
    if all(is_number(v) for v in present):
        if any(h in name for h in ID_HINTS):
            return "id"
        if any(h in name for h in TIME_HINTS) and all(float(v).is_integer() for v in present):
            return "period"
        return "number"
    if all(isinstance(v, str) for v in present):
        return "category"
    return None


def pick(columns: list[str], rows: list[list[Any]]) -> tuple[str, str, str] | None:
    """('bar', category, number) or ('line', period or date, number); None when the shape does not fit."""
    if len(rows) < 2:
        return None
    kinds = {c: _kind(c, [r[i] for r in rows]) for i, c in enumerate(columns)}
    kept = [c for c in columns if kinds[c] != "id"]
    if len(kept) != 2:
        return None
    a, b = kept
    for x, y in ((a, b), (b, a)):
        if kinds[y] == "number" and kinds[x] == "category":
            return "bar", x, y
        if kinds[y] == "number" and kinds[x] in ("period", "date"):
            return "line", x, y
    return None


def build(columns: list[str], rows: list[list[Any]], money: set[str], height_per_bar: int = 28) -> alt.Chart | None:
    shape = pick(columns, rows)
    if shape is None:
        return None
    kind, xcol, ycol = shape
    xi, yi = columns.index(xcol), columns.index(ycol)
    frame = pd.DataFrame([{"x": r[xi].isoformat() if isinstance(r[xi], (date, datetime)) else r[xi],
                           "y": float(r[yi]), "label": format_value(r[yi], ycol, money)}
                          for r in rows if r[yi] is not None and r[xi] is not None])
    tooltip = [alt.Tooltip("x:N", title=xcol), alt.Tooltip("label:N", title=ycol)]
    if kind == "bar":
        total = len(frame)
        frame = frame.sort_values("y", ascending=False).head(MAX_BARS)
        title = f"{ycol} by {xcol}" + (f", top {MAX_BARS} of {total} returned rows" if total > MAX_BARS else "")
        base = alt.Chart(frame, title=alt.TitleParams(title, anchor="start", color=INK, fontSize=14, fontWeight=600))
        y = alt.Y("x:N", sort="-x", title=None, axis=alt.Axis(labelLimit=220, ticks=False, domain=False))
        x = alt.X("y:Q", title=None, axis=None, scale=alt.Scale(zero=True))
        hover = alt.selection_point(on="pointerover", fields=["x"], empty=False)
        bars = base.mark_bar(size=16, cornerRadiusEnd=4).encode(
            y=y, x=x, tooltip=tooltip, color=alt.condition(hover, alt.value(HIGHLIGHT), alt.value(ACCENT))).add_params(hover)
        labels = base.mark_text(align="left", dx=6, color=INK, fontSize=12).encode(y=y, x=x, text="label:N")
        chart = (bars + labels).properties(height=height_per_bar * len(frame)).interactive(bind_y=False)
    else:
        title = f"{ycol} by {xcol}"
        x_type = "T" if isinstance(rows[0][xi], (date, datetime)) else "O"
        base = alt.Chart(frame, title=alt.TitleParams(title, anchor="start", color=INK, fontSize=14, fontWeight=600))
        x = alt.X(f"x:{x_type}", title=xcol, axis=alt.Axis(labelAngle=0))
        y = alt.Y("y:Q", title=None, axis=alt.Axis(labelExpr="replace(format(datum.value, '.3~s'), 'G', 'B')"))
        hover = alt.selection_point(on="pointerover", nearest=True, fields=["x"], empty=False)
        line = base.mark_line(color=ACCENT, strokeWidth=2).encode(x=x, y=y)
        points = base.mark_point(filled=True, color=ACCENT).encode(
            x=x, y=y, tooltip=tooltip, size=alt.condition(hover, alt.value(150), alt.value(56))).add_params(hover)
        last = alt.Chart(frame.tail(1)).mark_text(align="left", dx=8, color=INK, fontSize=12)
        chart = (line + points + last.encode(x=x, y=y, text="label:N")).properties(height=260).interactive()
    return (chart.configure_view(strokeWidth=0)
            .configure_axis(grid=False, domainColor=RULE, tickColor=RULE, labelColor=MUTED, titleColor=MUTED, labelFontSize=12)
            .properties(background="transparent"))
