"""Cell-bound answers: every result value in the prose is a {{rN.COL}} pointer that code fills in and formats.

A plain numeral in the prose is accepted only if it appears in the question, equals the row count, a cell
value (raw or formatted), or an audit value (data coverage, a filter literal of the executed SQL).
Anything else, or a pointer to a cell that does not exist, switches to a deterministic answer.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .contracts import ExecutionResult, RenderedAnswer, Segment

PLACEHOLDER = re.compile(r"\{\{\s*(?:r(\d+)\.([A-Za-z_][A-Za-z0-9_$]*)|(row_count))\s*\}\}", re.I)
DATE_TOKEN = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")
NUMBER_TOKEN = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")
MONEY_HINTS = ("REVENUE", "SALES", "PRICE", "COST", "PROFIT", "BALANCE", "ACCTBAL", "AMOUNT", "VALUE", "SPEND")
PERCENT_HINTS = ("PCT", "PERCENT")


def format_value(value: Any, column: str, money_columns: set[str] | frozenset[str] = frozenset()) -> str:
    if value is None:
        return "no value"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float, Decimal)):
        number = _decimal(value)
        if number is None:
            return str(value)
        name = column.upper()
        if name in money_columns or any(h in name for h in MONEY_HINTS):
            return f"{number:,.2f}"
        if any(h in name for h in PERCENT_HINTS):
            return f"{number:,.2f}%"
        if number == number.to_integral_value():
            return f"{int(number):,}"
        return _trim(f"{number:,.4f}")
    return str(value)


def is_empty(result: ExecutionResult) -> bool:
    """No rows, or only NULLs (e.g. SUM over nothing)."""
    return result.row_count == 0 or all(v is None for row in result.rows for v in row)


def render_answer(template: str, result: ExecutionResult, question: str,
                  money_columns: set[str] | frozenset[str] = frozenset(),
                  audit_numbers: Iterable[Decimal] = (), audit_dates: Iterable[str] = ()) -> RenderedAnswer:
    columns = {c.upper(): c for c in result.columns}
    segments: list[Segment] = []
    problems: list[str] = []
    position = 0
    for match in PLACEHOLDER.finditer(template):
        if match.start() > position:
            segments.append(Segment(text=template[position:match.start()]))
        position = match.end()
        if match.group(3):
            segments.append(Segment(text=f"{result.row_count:,}", column="row_count"))
            continue
        row, column = int(match.group(1)), columns.get(match.group(2).upper())
        if column is None or row >= len(result.rows):
            problems.append(match.group(0))
            continue
        value = result.rows[row][result.columns.index(column)]
        segments.append(Segment(text=format_value(value, column, money_columns), row=row, column=column))
    if position < len(template):
        segments.append(Segment(text=template[position:]))

    prose = PLACEHOLDER.sub(" ", template)
    if "{{" in prose or "}}" in prose:
        problems.append("malformed placeholder")
    rejected = _unsupported_numbers(prose, result, question, money_columns, audit_numbers, audit_dates)
    if problems or rejected or not template.strip():
        return deterministic_answer(result, money_columns, rejected=[*problems, *rejected])
    return RenderedAnswer(segments=segments, grounded=True)


def deterministic_answer(result: ExecutionResult, money_columns: set[str] | frozenset[str] = frozenset(),
                         rejected: list[str] | None = None) -> RenderedAnswer:
    """Answer built by code alone from the result, used when the model's text cannot be verified."""
    rejected = rejected or []
    if is_empty(result):
        return RenderedAnswer(segments=[Segment(text="No data matched: the query ran in Snowflake and returned no values.")],
                              grounded=True, fallback_used=True, rejected_numbers=rejected)
    segments: list[Segment] = []
    if result.row_count == 1:
        segments.append(Segment(text="Result: "))
    else:
        segments += [Segment(text=f"{result.row_count:,}", column="row_count"), Segment(text=" rows. First row: ")]
    for i, column in enumerate(result.columns):
        if i:
            segments.append(Segment(text="; "))
        segments.append(Segment(text=f"{column} = "))
        segments.append(Segment(text=format_value(result.rows[0][i], column, money_columns), row=0, column=column))
    segments.append(Segment(text="." if result.row_count == 1 else ". The table shows every row."))
    return RenderedAnswer(segments=segments, grounded=True, fallback_used=True, rejected_numbers=rejected)


def _unsupported_numbers(prose: str, result: ExecutionResult, question: str, money_columns,
                         audit_numbers: Iterable[Decimal], audit_dates: Iterable[str]) -> list[str]:
    allowed_dates = set(DATE_TOKEN.findall(question)) | set(audit_dates)
    allowed = {_decimal(t) for t in NUMBER_TOKEN.findall(DATE_TOKEN.sub(" ", question))}
    allowed |= {Decimal(result.row_count), *audit_numbers}
    for row in result.rows:
        for column, value in zip(result.columns, row):
            if isinstance(value, (date, datetime)):
                allowed_dates.add(format_value(value, column))
            elif isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
                allowed.add(_decimal(value))
                allowed.add(_decimal(format_value(value, column, money_columns)))
    rejected = [d for d in DATE_TOKEN.findall(prose) if d not in allowed_dates]
    for token in NUMBER_TOKEN.findall(DATE_TOKEN.sub(" ", prose)):
        if _decimal(token) not in allowed:
            rejected.append(token)
    return rejected


def _decimal(value: Any) -> Decimal | None:
    try:
        if isinstance(value, Decimal):
            return value
        if isinstance(value, float):
            return Decimal(repr(value))
        return Decimal(str(value).replace(",", "").rstrip("%").strip())
    except (InvalidOperation, ValueError):
        return None


def _trim(text: str) -> str:
    return text.rstrip("0").rstrip(".") if "." in text else text
