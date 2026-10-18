"""Typed contracts shared by the pipeline, the UI and the eval runner.

Frozen at 1:15 PM: later changes are additive only (new optional fields), so teammates' code keeps working.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Status = Literal["ANSWER", "CLARIFY", "UNSUPPORTED", "UNSAFE"]


class Plan(BaseModel):
    """Gemma PLAN output: what the question means and the SQL that answers it."""

    status: Status
    standalone_question: str = Field(description="The question rewritten to stand alone (follow-ups resolved).")
    assumptions: list[str] = Field(default_factory=list, description="Interpretations chosen where the question was open.")
    clarification: str | None = Field(default=None, description="Question to ask the user when status is CLARIFY.")
    options: list[str] = Field(default_factory=list, description="Up to 3 rephrasings the user can pick when status is CLARIFY.")
    reason: str | None = Field(default=None, description="Why the request is UNSUPPORTED or UNSAFE.")
    sql: str | None = Field(default=None, description="One read-only Snowflake SELECT when status is ANSWER.")


class AnswerDraft(BaseModel):
    """Gemma EXPLAIN output: answer prose whose values are pointers like {{r0.REVENUE}}."""

    answer: str


class GateReport(BaseModel):
    ok: bool
    sql: str | None = None  # the exact SQL that may run (normalised, row cap applied)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    tables: list[str] = Field(default_factory=list)
    row_cap: int | None = None


class ExecutionResult(BaseModel):
    query_id: str
    sql: str
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    elapsed_ms: int
    executed_at: str
    role: str | None = None
    warehouse: str | None = None


class LLMCall(BaseModel):
    """Provenance of one Gemma call, shown next to every answer."""

    role: str  # plan | explain | insights
    tier: str  # gemini | ollama
    model: str
    latency_ms: int
    cached: bool = False
    fallback_used: bool = False
    prompt_tokens_est: int = 0
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    note: str | None = None


class Segment(BaseModel):
    """A piece of the rendered answer; value segments point at one result cell."""

    text: str
    row: int | None = None
    column: str | None = None


class RenderedAnswer(BaseModel):
    segments: list[Segment]
    grounded: bool
    fallback_used: bool = False
    rejected_numbers: list[str] = Field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.segments)


class Receipt(BaseModel):
    """Plain-language account of what ran, compiled from the executed SQL (never written by the model)."""

    tables: list[str] = Field(default_factory=list)
    joins: list[str] = Field(default_factory=list)
    filters: list[str] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    measures: list[str] = Field(default_factory=list)
    order_by: list[str] = Field(default_factory=list)
    limit: int | None = None
    assumptions: list[str] = Field(default_factory=list)


class InsightProposal(BaseModel):
    """Gemma INSIGHTS output, step 1: a follow-up analysis as SQL."""

    kind: Literal["trend", "share", "outlier", "comparison"]
    title: str
    sql: str


class InsightProposals(BaseModel):
    analyses: list[InsightProposal] = Field(default_factory=list)


class InsightFinding(BaseModel):
    index: int
    finding: str = ""


class InsightFindings(BaseModel):
    """Gemma INSIGHTS output, step 2: one {{rN.COL}} sentence per executed analysis."""

    findings: list[InsightFinding] = Field(default_factory=list)


class Insight(BaseModel):
    kind: str
    title: str
    sql: str
    query_id: str | None = None
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    answer: RenderedAnswer | None = None
    verified: bool = False
    reason: str | None = None  # why it was dropped


class InsightsReport(BaseModel):
    shown: list[Insight] = Field(default_factory=list)
    dropped: list[Insight] = Field(default_factory=list)
    llm_calls: list[LLMCall] = Field(default_factory=list)
    message: str | None = None


class Envelope(BaseModel):
    """Everything the UI needs for one question."""

    question: str
    profile: str
    status: Literal["ANSWER", "CLARIFY", "UNSUPPORTED", "UNSAFE", "ERROR"]
    message: str | None = None  # user-facing text for non-answer outcomes
    plan: Plan | None = None
    gate: GateReport | None = None
    execution: ExecutionResult | None = None
    answer: RenderedAnswer | None = None
    receipt: Receipt | None = None
    llm_calls: list[LLMCall] = Field(default_factory=list)
    timings_ms: dict[str, int] = Field(default_factory=dict)
    diagnostics: list[str] = Field(default_factory=list)  # e.g. how many rows each filter keeps when nothing matched
    diagnostic_query_id: str | None = None
    answer_template: str | None = None  # Gemma's {{rN.COL}} text, reused when the same SQL is re-run
    previous_query_id: str | None = None  # set on a re-run: the query this one replays
    changes: list[str] = Field(default_factory=list)  # cell-level differences from the previous run
