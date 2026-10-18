from datetime import date

from datamind.introspect import introspect


class FakeRunner:
    def __init__(self):
        self.sql = []

    def run_unchecked(self, sql):
        self.sql.append(sql)
        if "DISTINCT" in sql:
            return "qid-values", [("LG BOX",), ("SM CASE",)]
        n_dates = sql.count("MIN(")
        return "qid-dates", [tuple(v for _ in range(n_dates) for v in (date(1992, 1, 1), date(1998, 8, 2)))]


def test_introspect_collects_coverage_and_values(tpch):
    runner = FakeRunner()
    data = introspect(tpch, runner)
    assert data["coverage"]["ORDERS.O_ORDERDATE"] == ["1992-01-01", "1998-08-02"]
    assert set(data["values"]) == {"PART.P_TYPE", "PART.P_CONTAINER"}
    assert all(s.startswith("SELECT ") and "DATAMIND.TPCH." in s for s in runner.sql)
    assert len(runner.sql) == 4  # ORDERS dates, LINEITEM dates, two auto value lists
