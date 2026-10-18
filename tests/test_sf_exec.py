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


def test_unknown_plan_layout_is_conservative():
    assert cartesian_scan(["operation"], [("CartesianJoin",)])
