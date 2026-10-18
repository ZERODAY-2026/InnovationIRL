import pytest

from datamind.gate import check_sql

REVENUE_BY_NATION_1995 = """
SELECT n.N_NAME AS NATION, SUM(l.L_EXTENDEDPRICE * (1 - l.L_DISCOUNT)) AS REVENUE
FROM LINEITEM l
JOIN ORDERS o ON l.L_ORDERKEY = o.O_ORDERKEY
JOIN CUSTOMER c ON o.O_CUSTKEY = c.C_CUSTKEY
JOIN NATION n ON c.C_NATIONKEY = n.N_NATIONKEY
WHERE o.O_ORDERDATE >= '1995-01-01' AND o.O_ORDERDATE < '1996-01-01'
GROUP BY n.N_NAME
ORDER BY REVENUE DESC
LIMIT 5
"""

ALLOWED = {
    "single table": "SELECT N_NAME FROM NATION",
    "join with declared keys, top 5": REVENUE_BY_NATION_1995,
    "cte and qualify": """
        WITH c AS (SELECT O_CUSTKEY, COUNT(*) AS N FROM ORDERS GROUP BY O_CUSTKEY)
        SELECT C_NAME, N FROM c JOIN CUSTOMER ON c.O_CUSTKEY = CUSTOMER.C_CUSTKEY
        QUALIFY ROW_NUMBER() OVER (ORDER BY N DESC) <= 3""",
    "union all": "SELECT R_NAME AS NAME FROM REGION UNION ALL SELECT N_NAME FROM NATION",
    "fully qualified": "SELECT COUNT(*) AS N FROM DATAMIND.TPCH.ORDERS",
    "lower case": "select o_orderpriority, count(*) as n from orders group by o_orderpriority",
    "trailing semicolon": "SELECT R_NAME FROM REGION;",
    "customer and supplier in same nation (TPC-H Q5)": """
        SELECT N_NAME, SUM(L_EXTENDEDPRICE * (1 - L_DISCOUNT)) AS REVENUE
        FROM CUSTOMER, ORDERS, LINEITEM, SUPPLIER, NATION, REGION
        WHERE C_CUSTKEY = O_CUSTKEY AND L_ORDERKEY = O_ORDERKEY AND L_SUPPKEY = S_SUPPKEY
          AND C_NATIONKEY = S_NATIONKEY AND S_NATIONKEY = N_NATIONKEY AND N_REGIONKEY = R_REGIONKEY
          AND R_NAME = 'ASIA'
        GROUP BY N_NAME""",
    "count star": "SELECT COUNT(*) FROM LINEITEM",
    "count one": "SELECT COUNT(1) AS N FROM ORDERS",
    "self join": """
        SELECT COUNT(DISTINCT l1.L_ORDERKEY) AS N FROM LINEITEM l1
        JOIN LINEITEM l2 ON l1.L_ORDERKEY = l2.L_ORDERKEY AND l1.L_SUPPKEY <> l2.L_SUPPKEY""",
    "composite partsupp join": """
        SELECT SUM(L_EXTENDEDPRICE * (1 - L_DISCOUNT) - PS_SUPPLYCOST * L_QUANTITY) AS PROFIT
        FROM LINEITEM JOIN PARTSUPP ON L_PARTKEY = PS_PARTKEY AND L_SUPPKEY = PS_SUPPKEY""",
    "correlated subquery": """
        SELECT S_NAME, PS_SUPPLYCOST FROM PARTSUPP ps JOIN SUPPLIER s ON ps.PS_SUPPKEY = s.S_SUPPKEY
        WHERE ps.PS_SUPPLYCOST = (SELECT MIN(p2.PS_SUPPLYCOST) FROM PARTSUPP p2 WHERE p2.PS_PARTKEY = ps.PS_PARTKEY)""",
    "late lines share": """
        SELECT COUNT_IF(L_RECEIPTDATE > L_COMMITDATE) / NULLIF(COUNT(*), 0) AS LATE_SHARE FROM LINEITEM""",
    "year function and having": """
        SELECT YEAR(O_ORDERDATE) AS Y, COUNT(*) AS N FROM ORDERS GROUP BY Y HAVING COUNT(*) > 10 ORDER BY Y""",
    "top n syntax": "SELECT TOP 5 C_NAME, C_ACCTBAL FROM CUSTOMER ORDER BY C_ACCTBAL DESC",
    "ilike filter": "SELECT P_NAME FROM PART WHERE P_TYPE ILIKE '%brass%'",
    "numeric text and date text are fine": "SELECT COUNT(*) AS N FROM ORDERS WHERE O_CUSTKEY = '42' AND O_ORDERDATE >= '1995-01-01'",
    "difference of single-row totals": """
        SELECT a.R1997 - b.R1996 AS CHANGE FROM
        (SELECT SUM(L_EXTENDEDPRICE) AS R1997 FROM LINEITEM WHERE L_SHIPDATE >= '1997-01-01') a,
        (SELECT SUM(L_EXTENDEDPRICE) AS R1996 FROM LINEITEM WHERE L_SHIPDATE < '1997-01-01') b""",
}

REJECTED = {
    "drop": "DROP TABLE ORDERS",
    "delete": "DELETE FROM ORDERS",
    "update": "UPDATE ORDERS SET O_TOTALPRICE = 0",
    "insert": "INSERT INTO ORDERS SELECT * FROM ORDERS",
    "merge": "MERGE INTO ORDERS o USING ORDERS s ON o.O_ORDERKEY = s.O_ORDERKEY WHEN MATCHED THEN DELETE",
    "ctas": "CREATE TABLE X AS SELECT * FROM ORDERS",
    "alter": "ALTER TABLE ORDERS ADD COLUMN X INT",
    "grant": "GRANT SELECT ON TABLE ORDERS TO ROLE PUBLIC",
    "truncate": "TRUNCATE TABLE ORDERS",
    "stacked statements": "SELECT 1 FROM ORDERS; DROP TABLE ORDERS",
    "comment hides second statement": "SELECT * FROM ORDERS -- harmless\n; DELETE FROM ORDERS",
    "use role": "USE ROLE ACCOUNTADMIN",
    "call procedure": "CALL my_proc()",
    "copy into": "COPY INTO ORDERS FROM @stage",
    "put file": "PUT file://x.csv @~",
    "show": "SHOW TABLES",
    "execute immediate": "EXECUTE IMMEDIATE 'DROP TABLE ORDERS'",
    "system function": "SELECT SYSTEM$WAIT(10) FROM ORDERS",
    "result scan table function": "SELECT * FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))",
    "sample database": "SELECT * FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.ORDERS",
    "account usage": "SELECT * FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY",
    "information schema": "SELECT * FROM INFORMATION_SCHEMA.TABLES",
    "other database": "SELECT * FROM OTHERDB.TPCH.ORDERS",
    "stage scan": "SELECT $1 FROM @mystage",
    "unknown column": "SELECT O_NOPE FROM ORDERS",
    "undeclared key join": "SELECT COUNT(*) FROM NATION JOIN SUPPLIER ON N_NATIONKEY = S_SUPPKEY",
    "no table at all": "SELECT 42 AS ANSWER",
    "constants dressed as data": "SELECT 'GERMANY' AS NATION, 1000 AS REVENUE FROM NATION LIMIT 1",
    "select into": "SELECT * INTO NEW_T FROM ORDERS",
    "account metadata": "SELECT CURRENT_ACCOUNT(), N_NAME FROM NATION",
    "empty": "   ",
    "not sql": "hello world",
    "text compared with numeric key": "SELECT O_ORDERKEY FROM ORDERS WHERE O_CUSTKEY = 'Customer#999999999'",
    "text compared with date": "SELECT COUNT(*) AS N FROM ORDERS WHERE O_ORDERDATE > 'last year'",
    "hidden personal column": "SELECT C_NAME, C_PHONE FROM CUSTOMER",
    "star leaks hidden columns": "SELECT * FROM CUSTOMER LIMIT 5",
    "qualified star": "SELECT c.* FROM CUSTOMER c LIMIT 5",
}


@pytest.mark.parametrize("name", list(ALLOWED))
def test_allowed(tpch, name):
    report = check_sql(ALLOWED[name], tpch)
    assert report.ok, report.errors
    assert report.sql and "LIMIT" in report.sql.upper()


@pytest.mark.parametrize("name", list(REJECTED))
def test_rejected(tpch, name):
    report = check_sql(REJECTED[name], tpch)
    assert not report.ok
    assert report.errors and report.sql is None


def test_row_cap_added_when_missing(tpch):
    report = check_sql("SELECT N_NAME FROM NATION", tpch, max_rows=50)
    assert report.ok and report.row_cap == 50
    assert report.sql.rstrip().endswith("LIMIT 51")


def test_small_limit_kept(tpch):
    report = check_sql(REVENUE_BY_NATION_1995, tpch)
    assert report.sql.rstrip().endswith("LIMIT 5")
    assert not report.warnings


def test_large_limit_lowered_with_warning(tpch):
    report = check_sql("SELECT C_NAME FROM CUSTOMER LIMIT 5000", tpch, max_rows=100)
    assert report.ok and report.sql.rstrip().endswith("LIMIT 101")
    assert any("lowered" in w for w in report.warnings)


def test_comments_stripped_from_executed_sql(tpch):
    report = check_sql("SELECT /* note */ N_NAME FROM NATION -- trailing", tpch)
    assert report.ok and "note" not in report.sql and "trailing" not in report.sql


def test_tables_reported(tpch):
    report = check_sql(REVENUE_BY_NATION_1995, tpch)
    assert report.tables == ["CUSTOMER", "LINEITEM", "NATION", "ORDERS"]


def test_bad_join_error_lists_declared_joins(tpch):
    report = check_sql(REJECTED["undeclared key join"], tpch)
    assert any("declared joins:" in e for e in report.errors)
