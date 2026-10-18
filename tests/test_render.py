from datetime import date
from decimal import Decimal

from datamind.contracts import ExecutionResult
from datamind.render import deterministic_answer, format_value, is_empty, render_answer


def result(columns, rows):
    return ExecutionResult(query_id="01-test", sql="SELECT 1", columns=columns, rows=rows, row_count=len(rows),
                           truncated=False, elapsed_ms=5, executed_at="2026-10-18T08:00:00+00:00")


TOP5 = result(["NATION", "REVENUE"], [
    ["GERMANY", Decimal("1234567.891")], ["FRANCE", Decimal("1200000.5")], ["CHINA", Decimal("1100000")],
    ["INDIA", Decimal("1000000.25")], ["PERU", Decimal("999999.999")],
])
Q = "Which were the top 5 nations by revenue in 1995?"
MONEY = {"REVENUE"}


def test_top5_nations_by_revenue_1995_is_cell_bound():
    template = ("In 1995 the top 5 nations by revenue were led by {{r0.NATION}} with {{r0.REVENUE}}, "
                "followed by {{r1.NATION}}; the table lists all {{row_count}}.")
    ans = render_answer(template, TOP5, Q, MONEY)
    assert ans.grounded and not ans.fallback_used
    assert "GERMANY with 1,234,567.89, followed by FRANCE" in ans.text
    pointers = [(s.row, s.column) for s in ans.segments if s.row is not None]
    assert pointers == [(0, "NATION"), (0, "REVENUE"), (1, "NATION")]


def test_invented_number_triggers_deterministic_fallback():
    ans = render_answer("{{r0.NATION}} earned about 1.2 million.", TOP5, Q, MONEY)
    assert ans.fallback_used and "1.2" in ans.rejected_numbers
    assert "REVENUE = 1,234,567.89" in ans.text


def test_typed_cell_value_allowed_raw_or_formatted():
    assert not render_answer("GERMANY had 1,234,567.89 in revenue.", TOP5, Q, MONEY).fallback_used
    assert not render_answer("GERMANY had 1234567.891 in revenue.", TOP5, Q, MONEY).fallback_used


def test_question_numbers_allowed_others_not():
    assert not render_answer("The top 5 for 1995 start with {{r0.NATION}}.", TOP5, Q).fallback_used
    assert render_answer("The top 6 start with {{r0.NATION}}.", TOP5, Q).fallback_used


def test_bad_pointers_trigger_fallback():
    assert render_answer("{{r9.NATION}} leads.", TOP5, Q).fallback_used
    assert render_answer("{{r0.PROFIT}} leads.", TOP5, Q).fallback_used
    assert render_answer("{{r0.NATION} leads.", TOP5, Q).fallback_used


def test_audit_values_allowed():
    assert not render_answer("Since 1995-01-01, {{r0.NATION}} leads.", TOP5, "Top nation since 1995?",
                             audit_dates={"1995-01-01"}).fallback_used
    assert not render_answer("In 1998 {{r0.NATION}} led.", TOP5, "Who led last year?",
                             audit_numbers={Decimal(1998)}).fallback_used
    assert render_answer("In 1999 {{r0.NATION}} led.", TOP5, "Who led last year?",
                         audit_numbers={Decimal(1998)}).fallback_used


def test_formatting_has_no_float_noise():
    assert format_value(Decimal("1234567.891"), "REVENUE") == "1,234,567.89"
    assert format_value(0.1 + 0.2, "AVG_DISCOUNT") == "0.3"
    assert format_value(Decimal("150000"), "CUSTOMER_COUNT") == "150,000"
    assert format_value(date(1998, 8, 2), "LAST_ORDER") == "1998-08-02"
    assert format_value(None, "X") == "no value"
    assert format_value(Decimal("12.5"), "LATE_PCT") == "12.50%"
    assert format_value(Decimal("-5.5"), "C_ACCTBAL", {"C_ACCTBAL"}) == "-5.50"
    assert format_value(1998, "ORDER_YEAR") == "1998" and format_value(Decimal(1998), "Y") == "1998"
    assert format_value(Decimal(1234567), "O_ORDERKEY") == "1234567"


def test_empty_and_all_null_results():
    assert is_empty(result(["REVENUE"], []))
    nulls = result(["REVENUE"], [[None]])
    assert is_empty(nulls) and "No data matched" in deterministic_answer(nulls).text


def test_typed_names_and_unique_values_are_linked_to_cells():
    ans = render_answer("The top nations were GERMANY and FRANCE; GERMANY had 1,234,567.89.", TOP5, Q, MONEY)
    assert not ans.fallback_used and ans.text == "The top nations were GERMANY and FRANCE; GERMANY had 1,234,567.89."
    assert [(s.text, s.row, s.column) for s in ans.segments if s.row is not None] == [
        ("GERMANY", 0, "NATION"), ("FRANCE", 1, "NATION"), ("GERMANY", 0, "NATION"), ("1,234,567.89", 0, "REVENUE")]


def test_repeated_values_are_not_linked_to_a_guessed_cell():
    shares = result(["SHIP_MODE", "AVG_DISCOUNT"], [["AIR", Decimal("0.05")], ["SHIP", Decimal("0.05")]])
    ans = render_answer("SHIP averages 0.05.", shares, "Average discount by ship mode?")
    assert [(s.text, s.row) for s in ans.segments if s.row is not None] == [("SHIP", 1)]


def test_fallback_for_many_rows_points_at_cells():
    ans = deterministic_answer(TOP5, MONEY)
    assert ans.text.startswith("5 rows. First row: NATION = GERMANY; REVENUE = 1,234,567.89")
    assert {(s.row, s.column) for s in ans.segments if s.row is not None} == {(0, "NATION"), (0, "REVENUE")}
