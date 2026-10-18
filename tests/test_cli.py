from decimal import Decimal

from datamind.__main__ import restore_types
from datamind.contracts import Envelope, ExecutionResult
from datamind.pipeline import diff_results


def test_saved_envelope_round_trip_keeps_diff_exact():
    x = ExecutionResult(query_id="q1", sql="SELECT 1", columns=["NATION", "REVENUE"],
                        rows=[["JAPAN", Decimal("1370004628.75")], ["PERU", None]], row_count=2,
                        truncated=False, elapsed_ms=1, executed_at="t")
    env = Envelope(question="q", profile="tpch", status="ANSWER", execution=x)
    restored = restore_types(Envelope.model_validate_json(env.model_dump_json()))
    assert restored.execution.rows == [["JAPAN", Decimal("1370004628.75")], ["PERU", None]]
    assert diff_results(restored.execution, x, set()) == ["No differences: the data behind this answer has not changed."]
