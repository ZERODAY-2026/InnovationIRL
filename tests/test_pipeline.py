from decimal import Decimal

import pytest

from datamind.contracts import ExecutionResult, LLMCall
from datamind.gate import check_sql
from datamind.gemma import ModelError
from datamind.pipeline import DataMind
from datamind.prompts import plan_prompts
from datamind.sf_exec import QueryError

GOOD_SQL = ("SELECT n.N_NAME AS NATION, SUM(l.L_EXTENDEDPRICE * (1 - l.L_DISCOUNT)) AS REVENUE FROM LINEITEM l "
            "JOIN ORDERS o ON l.L_ORDERKEY = o.O_ORDERKEY JOIN CUSTOMER c ON o.O_CUSTKEY = c.C_CUSTKEY "
            "JOIN NATION n ON c.C_NATIONKEY = n.N_NATIONKEY WHERE o.O_ORDERDATE >= '1995-01-01' "
            "AND o.O_ORDERDATE < '1996-01-01' GROUP BY n.N_NAME ORDER BY REVENUE DESC LIMIT 5")
Q = "Which 5 nations had the highest revenue in 1995?"


def plan(status="ANSWER", sql=GOOD_SQL, **extra):
    return {"status": status, "standalone_question": Q, "assumptions": ["Nation means the customer's nation."],
            "sql": sql if status == "ANSWER" else None, **extra}


class FakeGemma:
    def __init__(self, plans, answers=()):
        self.plans, self.answers, self.calls = list(plans), list(answers), []

    def generate_json(self, role, system, user_levels, schema, max_output_tokens=800, allow_cloud=True):
        self.calls.append({"role": role, "prompt": user_levels if isinstance(user_levels, str) else user_levels[0],
                           "allow_cloud": allow_cloud})
        item = (self.plans if role == "plan" else self.answers).pop(0)
        if isinstance(item, Exception):
            raise item
        return schema.model_validate(item), LLMCall(role=role, tier="gemini", model="fake", latency_ms=1)


class FakeRunner:
    def __init__(self, rows=None, explain=()):
        self.rows = rows if rows is not None else [["GERMANY", Decimal("1234567.891")], ["FRANCE", Decimal("1200000.5")]]
        self.explain_results, self.executed = list(explain), []

    def explain(self, sql):
        item = self.explain_results.pop(0) if self.explain_results else []
        if isinstance(item, Exception):
            raise item
        return item

    def execute(self, sql, max_rows):
        self.executed.append(sql)
        return ExecutionResult(query_id="01b-fake", sql=sql, columns=["NATION", "REVENUE"], rows=self.rows,
                               row_count=len(self.rows), truncated=False, elapsed_ms=3,
                               executed_at="2026-10-18T08:00:00+00:00", role="DATAMIND_READER", warehouse="DATAMIND_WH")


def make(tpch, plans, answers=(), runner=None):
    gemma = FakeGemma(plans, answers)
    runner = runner or FakeRunner()
    return DataMind(settings=object(), profile=tpch, gemma=gemma, runner=runner), gemma, runner


def test_answer_end_to_end(tpch):
    dm, gemma, runner = make(tpch, [plan()], [{"answer": "{{r0.NATION}} led 1995 with {{r0.REVENUE}}."}])
    env = dm.ask(Q)
    assert env.status == "ANSWER" and env.execution.query_id == "01b-fake"
    assert env.answer.text == "GERMANY led 1995 with 1,234,567.89." and not env.answer.fallback_used
    assert env.receipt.limit == 5 and "LIMIT 5" in runner.executed[0]
    assert [c.role for c in env.llm_calls] == ["plan", "explain"]


@pytest.mark.parametrize("status,needle", [("UNSAFE", "only reads data"), ("UNSUPPORTED", "cannot answer")])
def test_refusals_never_reach_snowflake(tpch, status, needle):
    dm, _, runner = make(tpch, [plan(status, reason="Asked to delete orders.")])
    env = dm.ask("delete all orders")
    assert env.status == status and needle in env.message and runner.executed == []


def test_clarify_returns_options(tpch):
    dm, _, _ = make(tpch, [plan("CLARIFY", clarification="Which numbers?", options=["Revenue by year?", "Orders by year?"])])
    env = dm.ask("numbers for Germany")
    assert env.status == "CLARIFY" and env.message == "Which numbers?" and len(env.plan.options) == 2


def test_gate_rejection_is_repaired(tpch):
    dm, gemma, runner = make(tpch, [plan(sql="DELETE FROM ORDERS"), plan()], [{"answer": "{{r0.NATION}} leads."}])
    env = dm.ask(Q)
    assert env.status == "ANSWER" and len(runner.executed) == 1
    assert "was rejected" in gemma.calls[1]["prompt"] and "DELETE" in gemma.calls[1]["prompt"]


def test_gives_up_after_three_bad_plans(tpch):
    dm, _, runner = make(tpch, [plan(sql="SELECT NOPE FROM ORDERS")] * 3)
    env = dm.ask(Q)
    assert env.status == "ERROR" and "after 3 attempts" in env.message and runner.executed == []


def test_snowflake_compile_error_is_repaired(tpch):
    runner = FakeRunner(explain=[QueryError("SQL compilation error: invalid identifier 'X'"), []])
    dm, gemma, _ = make(tpch, [plan(), plan()], [{"answer": "{{r0.NATION}} leads."}], runner=runner)
    env = dm.ask(Q)
    assert env.status == "ANSWER" and "invalid identifier" in gemma.calls[1]["prompt"]


def test_cartesian_join_is_repaired(tpch):
    runner = FakeRunner(explain=[["the query plan contains a cartesian join"], []])
    dm, gemma, _ = make(tpch, [plan(), plan()], [{"answer": "{{r0.NATION}} leads."}], runner=runner)
    assert dm.ask(Q).status == "ANSWER" and "cartesian" in gemma.calls[1]["prompt"]


def test_empty_result_skips_explain_model(tpch):
    dm, gemma, _ = make(tpch, [plan()], runner=FakeRunner(rows=[]))
    env = dm.ask(Q)
    assert env.status == "ANSWER" and "No data matched" in env.answer.text
    assert [c["role"] for c in gemma.calls] == ["plan"]


def test_model_unavailable_is_reported(tpch):
    dm, _, _ = make(tpch, [ModelError("Gemini rate limit (429)")])
    env = dm.ask(Q)
    assert env.status == "ERROR" and "429" in env.message


def test_explain_failure_falls_back_to_deterministic_answer(tpch):
    dm, _, _ = make(tpch, [plan()], [ModelError("timeout")])
    env = dm.ask(Q)
    assert env.status == "ANSWER" and env.answer.fallback_used and "NATION = GERMANY" in env.answer.text


def test_private_profile_keeps_calls_local(tpch):
    tpch_private = tpch.__class__(**{**tpch.__dict__, "cloud_allowed": False})
    dm, gemma, _ = make(tpch_private, [plan()], [{"answer": "{{r0.NATION}} leads."}])
    dm.ask(Q)
    assert all(call["allow_cloud"] is False for call in gemma.calls)


def test_outside_coverage_adds_honest_note(tpch):
    covered = tpch.__class__(**{**tpch.__dict__, "coverage": {"ORDERS.O_ORDERDATE": ("1992-01-01", "1998-08-02")}})
    sql = ("SELECT COUNT(*) AS N FROM ORDERS WHERE O_ORDERDATE >= '2005-01-01' AND O_ORDERDATE < '2006-01-01'")
    runner = FakeRunner(rows=[[Decimal(0)]])
    dm, _, _ = make(covered, [plan(sql=sql)], [{"answer": "There were {{r0.NATION}} orders."}], runner=runner)
    env = dm.ask("How many orders were placed in 2005?")
    assert env.status == "ANSWER"
    assert "only covers ORDERS.O_ORDERDATE from 1992-01-01 to 1998-08-02" in env.answer.text
    assert "2005-01-01" in env.answer.text


def test_inside_coverage_has_no_note(tpch):
    covered = tpch.__class__(**{**tpch.__dict__, "coverage": {"ORDERS.O_ORDERDATE": ("1992-01-01", "1998-08-02")}})
    dm, _, _ = make(covered, [plan()], [{"answer": "{{r0.NATION}} leads."}])
    assert "only covers" not in dm.ask(Q).answer.text


def test_profile_examples_pass_the_gate(tpch):
    answers = [e for e in tpch.examples if e["plan"]["status"] == "ANSWER"]
    assert len(answers) >= 3
    for example in answers:
        report = check_sql(example["plan"]["sql"], tpch)
        assert report.ok, (example["question"], report.errors)


def test_plan_prompt_levels_shrink(tpch):
    sizes = [len(p) for p in plan_prompts(tpch, Q)]
    assert sizes == sorted(sizes, reverse=True) and sizes[-1] < sizes[0] / 2
