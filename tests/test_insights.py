from decimal import Decimal

from datamind.contracts import Envelope, ExecutionResult, LLMCall, Plan
from datamind.insights import dig_deeper, verify_finding
from datamind.pipeline import DataMind
from tests.test_pipeline import FakeGemma


def result(columns, rows, sql="SELECT 1"):
    return ExecutionResult(query_id="qid", sql=sql, columns=columns, rows=rows, row_count=len(rows),
                           truncated=False, elapsed_ms=1, executed_at="t")


class ScriptedRunner:
    """Returns the next (columns, rows) for each executed query."""

    def __init__(self, outputs):
        self.outputs, self.executed = list(outputs), []

    def explain(self, sql):
        return []

    def execute(self, sql, max_rows):
        self.executed.append(sql)
        columns, rows = self.outputs.pop(0)
        return ExecutionResult(query_id=f"ins-{len(self.executed)}", sql=sql, columns=columns, rows=rows,
                               row_count=len(rows), truncated=False, elapsed_ms=1, executed_at="t")


def answered():
    x = result(["NATION", "REVENUE"], [["INDONESIA", Decimal("1370004628.75")]])
    plan = Plan(status="ANSWER", standalone_question="Top nations by revenue in 1995?", sql="SELECT 1")
    return Envelope(question="Top nations by revenue in 1995?", profile="tpch", status="ANSWER", plan=plan, execution=x)


def test_only_verified_findings_are_shown(tpch):
    proposals = {"analyses": [
        {"kind": "trend", "title": "Order value by year",
         "sql": "SELECT YEAR(O_ORDERDATE) AS Y, SUM(O_TOTALPRICE) AS VALUE_TOTAL, "
                "SUM(O_TOTALPRICE) / NULLIF(LAG(SUM(O_TOTALPRICE)) OVER (ORDER BY YEAR(O_ORDERDATE)), 0) * 100 - 100 AS PCT_CHANGE "
                "FROM ORDERS GROUP BY Y ORDER BY Y DESC"},
        {"kind": "share", "title": "Delete", "sql": "DELETE FROM ORDERS"},
        {"kind": "outlier", "title": "Customers per nation",
         "sql": "SELECT N_NAME, COUNT(*) AS CUSTOMERS FROM CUSTOMER JOIN NATION ON C_NATIONKEY = N_NATIONKEY GROUP BY N_NAME"},
    ]}
    findings = {"findings": [
        {"index": 0, "finding": "Order value grew by {{r0.PCT_CHANGE}} in {{r0.Y}}."},
        {"index": 1, "finding": "{{r0.N_NAME}} has about 42 thousand customers."},
    ]}
    runner = ScriptedRunner([(["Y", "VALUE_TOTAL", "PCT_CHANGE"], [[1998, Decimal("10"), Decimal("5.5")]]),
                             (["N_NAME", "CUSTOMERS"], [["CHINA", 6024]])])
    dm = DataMind(settings=object(), profile=tpch, gemma=FakeGemma([], [proposals, findings]), runner=runner)
    dm.gemma.plans = []
    dm.gemma.answers = [proposals, findings]
    report = dig_deeper(dm, answered())
    assert [i.title for i in report.shown] == ["Order value by year"]
    assert report.shown[0].answer.text == "Order value grew by 5.50% in 1998." and report.shown[0].query_id == "ins-1"
    reasons = {i.title: i.reason for i in report.dropped}
    assert reasons["Delete"].startswith("rejected by the SQL gate")
    assert reasons["Customers per nation"].startswith("unverified numbers")
    assert len(runner.executed) == 2  # the rejected SQL never reached Snowflake


def test_direction_words_must_match_the_sign():
    falling = result(["YEAR", "PCT_CHANGE"], [[1998, Decimal("-3.2")]])
    assert isinstance(verify_finding("Sales grew by {{r0.PCT_CHANGE}}.", "t", falling, set()), str)
    assert not isinstance(verify_finding("Sales fell by {{r0.PCT_CHANGE}}.", "t", falling, set()), str)
    assert verify_finding("Sales increased in {{r0.YEAR}}.", "t", falling, set()) == "trend words without a cited change value"


def test_superlatives_must_cite_the_first_row():
    shares = result(["N_NAME", "SHARE_PCT"], [["CHINA", Decimal("4.1")], ["PERU", Decimal("3.9")]])
    assert not isinstance(verify_finding("{{r0.N_NAME}} has the largest share, {{r0.SHARE_PCT}}.", "t", shares, set()), str)
    assert isinstance(verify_finding("{{r1.N_NAME}} has the largest share.", "t", shares, set()), str)


def test_needs_an_answered_question(tpch):
    dm = DataMind(settings=object(), profile=tpch, gemma=FakeGemma([]), runner=ScriptedRunner([]))
    assert dig_deeper(dm, Envelope(question="q", profile="tpch", status="UNSAFE")).message == "Insights need an answered question."
