from datamind.sf_exec import cartesian_scan

COLS = ["step", "id", "parent", "operation"]


def test_cross_join_of_single_row_aggregates_is_fine():
    rows = [(1, 0, None, "Result"), (1, 1, 0, "CartesianJoin"), (1, 2, 1, "Aggregate"), (1, 3, 2, "TableScan"),
            (1, 4, 1, "Aggregate"), (1, 5, 4, "Filter"), (1, 6, 5, "TableScan")]
    assert not cartesian_scan(COLS, rows)


def test_cross_join_of_table_rows_is_flagged():
    rows = [(1, 0, None, "Result"), (1, 1, 0, "CartesianJoin"), (1, 2, 1, "TableScan"),
            (1, 3, 1, "Filter"), (1, 4, 3, "TableScan")]
    assert cartesian_scan(COLS, rows)


def test_plans_without_cartesian_join_pass():
    assert not cartesian_scan(COLS, [(1, 0, None, "Result"), (1, 1, 0, "InnerJoin"), (1, 2, 1, "TableScan")])


def test_real_snowflake_plan_with_cte_self_join_passes():
    # EXPLAIN USING TABULAR output for "revenue 1997 minus 1996" via a CTE joined to itself (live, 18 Oct 2026)
    cols = ["step", "id", "parentOperators", "operation"]
    rows = [(None, None, None, "GlobalStats"), (1, 0, None, "Result"), (1, 1, "[0]", "Limit"),
            (1, 2, "[1]", "CartesianJoin"), (1, 3, "[2]", "WithReference"), (1, 4, "[3]", "Filter"),
            (1, 5, "[4, 13]", "WithClause"), (1, 6, "[5]", "Aggregate"), (1, 7, "[6]", "InnerJoin"),
            (1, 8, "[7]", "Filter"), (1, 9, "[8]", "TableScan"), (1, 10, "[7]", "JoinFilter"),
            (1, 11, "[10]", "TableScan"), (1, 12, "[2]", "WithReference"), (1, 13, "[12]", "Filter")]
    assert not cartesian_scan(cols, rows)
    flagged = [(1, 0, None, "Result"), (1, 1, "[0]", "CartesianJoin"), (1, 2, "[1]", "TableScan"), (1, 3, "[1]", "TableScan")]
    assert cartesian_scan(cols, flagged)


def test_unknown_plan_layout_is_conservative():
    assert cartesian_scan(["operation"], [("CartesianJoin",)])
