from decimal import Decimal

from datamind.gate import check_sql
from datamind.receipt import build_receipt, filter_literals, period_filters, zero_row_probe

SQL = """
SELECT n.N_NAME AS NATION, SUM(l.L_EXTENDEDPRICE * (1 - l.L_DISCOUNT)) AS REVENUE
FROM LINEITEM l
JOIN ORDERS o ON l.L_ORDERKEY = o.O_ORDERKEY
JOIN CUSTOMER c ON o.O_CUSTKEY = c.C_CUSTKEY
JOIN NATION n ON c.C_NATIONKEY = n.N_NATIONKEY
WHERE o.O_ORDERDATE >= '1995-01-01' AND o.O_ORDERDATE < '1996-01-01'
GROUP BY n.N_NAME ORDER BY REVENUE DESC LIMIT 5
"""


def test_receipt_describes_what_ran(tpch):
    gate = check_sql(SQL, tpch)
    r = build_receipt(gate.sql, tpch, ["Nation means the customer's nation."], gate.row_cap)
    assert len(r.tables) == 4 and r.tables[0].startswith("LINEITEM (Order lines")
    assert len(r.joins) == 3 and any("O_CUSTKEY = c.C_CUSTKEY" in j for j in r.joins)
    assert r.filters == ["o.O_ORDERDATE >= '1995-01-01'", "o.O_ORDERDATE < '1996-01-01'"]
    assert r.group_by == ["n.N_NAME"]
    assert r.measures[0].startswith("REVENUE = SUM(") and "metric 'revenue'" in r.measures[0]
    assert r.order_by == ["REVENUE DESC"] and r.limit == 5
    assert r.assumptions == ["Nation means the customer's nation."]


def test_display_cap_is_not_reported_as_user_limit(tpch):
    gate = check_sql("SELECT N_NAME FROM NATION", tpch)
    assert build_receipt(gate.sql, tpch, [], gate.row_cap).limit is None


def test_implicit_join_separated_from_filters(tpch):
    gate = check_sql("SELECT COUNT(*) AS N FROM ORDERS, CUSTOMER WHERE O_CUSTKEY = C_CUSTKEY AND C_MKTSEGMENT = 'BUILDING'", tpch)
    r = build_receipt(gate.sql, tpch, [], gate.row_cap)
    assert r.joins == ["O_CUSTKEY = C_CUSTKEY"] and r.filters == ["C_MKTSEGMENT = 'BUILDING'"]


def test_zero_row_probe_counts_each_filter(tpch):
    sql = ("SELECT O_ORDERKEY FROM ORDERS JOIN CUSTOMER ON O_CUSTKEY = C_CUSTKEY "
           "WHERE C_NAME = 'Customer#999999999' AND O_ORDERSTATUS = 'O' ORDER BY O_ORDERKEY LIMIT 10")
    probe_sql, labels = zero_row_probe(sql)
    assert labels == ["C_NAME = 'Customer#999999999'", "O_ORDERSTATUS = 'O'"]
    assert "COUNT_IF(C_NAME = 'Customer#999999999') AS F0" in probe_sql and "JOIN CUSTOMER" in probe_sql
    assert "ORDER BY" not in probe_sql and "LIMIT" not in probe_sql
    assert check_sql(probe_sql, tpch).ok


def test_zero_row_probe_keeps_implicit_joins(tpch):
    probe_sql, labels = zero_row_probe("SELECT O_ORDERKEY FROM ORDERS, CUSTOMER WHERE O_CUSTKEY = C_CUSTKEY AND C_NAME = 'x'")
    assert labels == ["C_NAME = 'x'"] and "WHERE O_CUSTKEY = C_CUSTKEY" in probe_sql
    assert zero_row_probe("SELECT O_ORDERKEY FROM ORDERS") is None


def test_period_filters_find_dates_and_years():
    dates, years = period_filters("SELECT COUNT(*) FROM ORDERS WHERE YEAR(O_ORDERDATE) = 2005 OR O_ORDERDATE < '1992-01-01'")
    assert dates == ["1992-01-01"] and years == [2005]
    assert period_filters("SELECT COUNT(*) FROM ORDERS WHERE YEAR(O_ORDERDATE) IN (1993, 1994)")[1] == [1993, 1994]


def test_filter_literals_exclude_limit():
    numbers, dates = filter_literals("SELECT 1 FROM ORDERS WHERE O_ORDERDATE >= '1998-01-01' AND O_SHIPPRIORITY = 0 LIMIT 5")
    assert dates == {"1998-01-01"} and Decimal(1998) in numbers and Decimal(0) in numbers
    assert Decimal(5) not in numbers
