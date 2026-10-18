from datetime import date
from decimal import Decimal

from datamind.contracts import Envelope, ExecutionResult
from datamind.evaluate import compare_results, numbers_match, score


def test_numbers_match_tolerance_and_rounding():
    assert numbers_match(Decimal("1370004628.75"), Decimal("1370004628.7512"))
    assert numbers_match(Decimal("0.63"), Decimal("0.63281"))  # legitimately rounded to 2 places
    assert not numbers_match(Decimal("0.60"), Decimal("0.63281"))
    assert not numbers_match(Decimal("150000"), Decimal("149999"))


def test_extra_columns_and_column_order_are_fine():
    gold = (["N_NAME", "REVENUE"], [["FRANCE", Decimal("10.5")], ["CHINA", Decimal("20.25")]])
    system = (["REVENUE", "NATION", "ORDERS"], [[Decimal("20.25"), "CHINA", 3], [Decimal("10.5"), "FRANCE", 7]])
    assert compare_results(*system, *gold)[0]


def test_wrong_values_or_rows_fail():
    gold = (["N"], [[Decimal(150000)]])
    assert not compare_results(["N"], [[Decimal(149999)]], *gold)[0]
    assert not compare_results(["N"], [[Decimal(150000)], [Decimal(1)]], *gold)[0]


def test_rows_must_line_up_across_columns():
    gold = (["NAME", "V"], [["A", 1], ["B", 2]])
    assert not compare_results(["NAME", "V"], [["A", 2], ["B", 1]], *gold)[0]


def test_order_matters_for_rankings():
    gold = (["NAME", "V"], [["A", 2], ["B", 1]])
    assert compare_results(["NAME", "V"], [["B", 1], ["A", 2]], *gold, order_matters=False)[0]
    assert not compare_results(["NAME", "V"], [["B", 1], ["A", 2]], *gold, order_matters=True)[0]


def test_percent_versus_fraction_and_partial_match():
    assert compare_results(["SHARE"], [[Decimal("0.2512")]], ["PCT"], [[Decimal("25.12")]])[0]
    monthly = (["M", "REVENUE"], [[date(1998, 1, 1), Decimal(5)], [date(1998, 2, 1), Decimal(7)]])
    assert compare_results(["MONTH_NO", "REVENUE"], [[1, Decimal(5)], [2, Decimal(7)]], *monthly, match=[1])[0]


def _env(status, rows=None, executed=True):
    execution = ExecutionResult(query_id="q", sql="SELECT 1", columns=["N"], rows=rows or [], row_count=len(rows or []),
                                truncated=False, elapsed_ms=1, executed_at="t") if executed else None
    return Envelope(question="q", profile="tpch", status=status, execution=execution)


def test_score_refusals_and_accepted_statuses():
    assert score({"expect": "refuse"}, _env("UNSAFE", executed=False), None)[0]
    assert not score({"expect": "refuse"}, _env("ANSWER", [[1]]), None)[0]
    assert score({"expect": "answer", "accept": ["CLARIFY"]}, _env("CLARIFY", executed=False), (["N"], [[1]]))[0]
    assert score({"expect": "empty"}, _env("ANSWER", []), None)[0]
    assert score({"expect": "answer"}, _env("ANSWER", [[Decimal(0)]]), (["N"], [[Decimal(0)]]))[0]
