"""Dashboard tests with a fake engine: no Gemma or Snowflake calls."""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

from datamind.contracts import (Envelope, ExecutionResult, GateReport, LLMCall, Plan, Receipt, RenderedAnswer,
                                Segment)

SQL = "SELECT N_NAME, SUM(L_EXTENDEDPRICE) AS REVENUE FROM NATION GROUP BY N_NAME ORDER BY REVENUE DESC LIMIT 2"


class FakeRunner:
    role, warehouse = "DATAMIND_APP", "DATAMIND_WH"

    def run_trusted(self, sql: str):
        if "COUNT(*)" in sql:
            return "qid-count", ["TABLE_NAME", "ROW_COUNT"], [(n, 25) for n in re.findall(r"SELECT '(\w+)'", sql)]
        return "qid-sample", ["A"], [("x",)]


class FakeDM:
    def __init__(self, profile, envs: dict[str, Envelope]):
        self.profile, self.envs, self.runner = profile, envs, FakeRunner()
        self.settings = SimpleNamespace(gemma_provider="gemini", gemini_model_for=lambda role: "gemma-test",
                                        snowflake_ready=True)

    def ask(self, question, history=None):
        return self.envs[question]

    def rerun(self, env):
        new = env.model_copy(deep=True)
        new.execution.query_id, new.previous_query_id, new.changes = "qid-rerun", env.execution.query_id, []
        return new


def answer_env(question: str, rows: list, diagnostics: list[str] | None = None) -> Envelope:
    x = ExecutionResult(query_id="qid-answer", sql=SQL, columns=["N_NAME", "REVENUE"], rows=rows, row_count=len(rows),
                        truncated=False, elapsed_ms=812, executed_at="2026-10-18 17:00:00", role="R", warehouse="W")
    segments = ([Segment(text="JAPAN", row=0, column="N_NAME"), Segment(text=" had the most revenue.")] if rows
                else [Segment(text="No rows matched.")])
    return Envelope(question=question, profile="tpch", status="ANSWER",
                    plan=Plan(status="ANSWER", standalone_question=question, sql=SQL),
                    gate=GateReport(ok=True, sql=SQL, tables=["NATION"], row_cap=1000), execution=x,
                    answer=RenderedAnswer(segments=segments, grounded=True), receipt=Receipt(tables=["NATION"]),
                    llm_calls=[LLMCall(role="plan", tier="gemini", model="gemma-test", latency_ms=900)],
                    timings_ms={"plan_ms": 900, "gate_ms": 5, "explain_ms": 300, "execute_ms": 820},
                    diagnostics=diagnostics or [], diagnostic_query_id="qid-diag" if diagnostics else None)


@pytest.fixture
def envs():
    return {
        "top": answer_env("top", [["JAPAN", 200.5], ["CHINA", 100.25]]),
        "empty": answer_env("empty", [], ["O_ORDERDATE in 1999 keeps 0 of 1,500,000 rows"]),
        "drop": Envelope(question="drop", profile="tpch", status="UNSAFE", message="It asks to delete data.",
                         plan=Plan(status="UNSAFE", standalone_question="drop", reason="It asks to delete data.")),
        "vague": Envelope(question="vague", profile="tpch", status="CLARIFY", message="Which year do you mean?",
                          plan=Plan(status="CLARIFY", standalone_question="vague", clarification="Which year?",
                                    options=["In 1995", "In 1996"])),
    }


def ask(tpch, envs, question: str) -> AppTest:
    at = AppTest.from_file("../app/main.py", default_timeout=30)
    at.session_state["engine"] = FakeDM(tpch, envs)
    at.run()
    at.text_area(key="question").input(question)
    at.button(key="ask").click()
    return at.run()


def text(at: AppTest) -> str:
    return " ".join(m.value for m in at.markdown)


def test_empty_state_and_data_tab(tpch, envs):
    at = AppTest.from_file("../app/main.py", default_timeout=30)
    at.session_state["engine"] = FakeDM(tpch, envs)
    at.run()
    assert not at.exception
    page = text(at)
    assert "No question asked yet" in page
    assert "DATAMIND.TPCH.LINEITEM" in page and "25 rows" in page and "qid-count" in page


def test_happy_path_shows_answer_sql_and_query_id(tpch, envs):
    at = ask(tpch, envs, "top")
    assert not at.exception
    page = text(at)
    assert "<b>JAPAN</b> had the most revenue." in page
    assert "qid-answer" in page and "1. Understood" in " ".join(e.label for e in at.expander)
    assert any(c.value == SQL for c in at.code)


def test_refusal_says_nothing_ran(tpch, envs):
    at = ask(tpch, envs, "drop")
    assert not at.exception
    assert "Nothing was executed in Snowflake." in text(at) and not at.code


def test_clarify_offers_reply_chips(tpch, envs):
    at = ask(tpch, envs, "vague")
    assert not at.exception
    assert "Which year do you mean?" in text(at)
    assert {"In 1995", "In 1996"} <= {b.label for b in at.button}


def test_zero_rows_shows_diagnosis(tpch, envs):
    at = ask(tpch, envs, "empty")
    assert not at.exception
    page = text(at)
    assert "Why no rows matched" in page and "qid-diag" in page and "keeps 0 of 1,500,000 rows" in page
