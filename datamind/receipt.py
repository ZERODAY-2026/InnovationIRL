"""Receipt: a plain account of what ran, compiled from the executed SQL (never written by the model)."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

import sqlglot
from sqlglot import exp

from .contracts import Receipt
from .profile import Profile

_QUALIFIER = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\.(?=[A-Za-z_])")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def build_receipt(sql: str, profile: Profile, assumptions: list[str], row_cap: int | None) -> Receipt:
    tree = sqlglot.parse_one(sql, read="snowflake")
    receipt = Receipt(assumptions=list(assumptions))
    for table in tree.find_all(exp.Table):
        name = (table.name or "").upper()
        if name in profile.tables:
            label = f"{name} ({profile.tables[name].description.rstrip('.')})"
            if label not in receipt.tables:
                receipt.tables.append(label)
    for join in tree.find_all(exp.Join):
        on = join.args.get("on")
        if on is not None:
            receipt.joins.append(f"{join.this.name.upper()} on {_sql(on)}")
    for select in tree.find_all(exp.Select):
        where = select.args.get("where")
        if where is not None:
            for part in _conjuncts(where.this):
                (receipt.joins if _is_column_equality(part) else receipt.filters).append(_sql(part))
        having = select.args.get("having")
        if having is not None:
            receipt.filters.append("after grouping: " + _sql(having.this))
        group = select.args.get("group")
        if group is not None:
            receipt.group_by.extend(_sql(e) for e in group.expressions)
    for projection in _top_selects(tree)[0].expressions if _top_selects(tree) else []:
        inner = projection.this if isinstance(projection, exp.Alias) else projection
        if inner.find(exp.AggFunc, exp.Window) or isinstance(inner, (exp.Binary, exp.Div)):
            receipt.measures.append(f"{projection.alias_or_name} = {_sql(inner)}{_metric_note(inner, profile)}")
    order = tree.args.get("order")
    if order is not None:
        receipt.order_by = [_sql(o) for o in order.expressions]
    limit = tree.args.get("limit")
    if limit is not None and isinstance(limit.expression, exp.Literal):
        value = int(limit.expression.name)
        receipt.limit = None if row_cap is not None and value == row_cap + 1 else value
    return receipt


def filter_literals(sql: str) -> tuple[set[Decimal], set[str]]:
    """Numbers and dates used as filter values in the executed SQL (audit values the answer may repeat)."""
    numbers: set[Decimal] = set()
    dates: set[str] = set()
    tree = sqlglot.parse_one(sql, read="snowflake")
    for clause in [*tree.find_all(exp.Where), *tree.find_all(exp.Having), *tree.find_all(exp.Qualify)]:
        for literal in clause.find_all(exp.Literal):
            text = literal.name
            if literal.is_string and _DATE.match(text):
                dates.add(text)
                numbers.add(Decimal(text[:4]))
            elif literal.is_number:
                try:
                    numbers.add(Decimal(text))
                except InvalidOperation:
                    pass
    return numbers, dates


def _top_selects(tree: exp.Expression) -> list[exp.Select]:
    if isinstance(tree, exp.Select):
        return [tree]
    return [s for s in tree.find_all(exp.Select) if s.parent_select is None]


def _conjuncts(node: exp.Expression) -> list[exp.Expression]:
    if isinstance(node, exp.And):
        return _conjuncts(node.left) + _conjuncts(node.right)
    if isinstance(node, exp.Paren):
        return _conjuncts(node.this)
    return [node]


def _is_column_equality(node: exp.Expression) -> bool:
    return isinstance(node, exp.EQ) and isinstance(node.left, exp.Column) and isinstance(node.right, exp.Column)


def _metric_note(expression: exp.Expression, profile: Profile) -> str:
    key = _normal(_sql(expression))
    for metric in profile.metrics.values():
        if _normal(metric.sql) == key:
            return f"  [metric '{metric.name}': {metric.desc}]" if metric.desc else f"  [metric '{metric.name}']"
    return ""


def _normal(text: str) -> str:
    return re.sub(r"\s+", "", _QUALIFIER.sub("", text)).upper()


def _sql(node: exp.Expression) -> str:
    return node.sql(dialect="snowflake", comments=False)
