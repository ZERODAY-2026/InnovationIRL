"""Verified Insights ("Dig deeper"): follow-up analyses that are shown only if every claim is backed by a query.

1. Gemma proposes at most 3 analyses (trend, share, outlier, comparison) as SQL that computes every number it
   might cite as an explicit column (PCT_CHANGE, DIFF, SHARE_PCT, Z_SCORE, RANK...).
2. Each SQL passes the same gate and runs in Snowflake, capped at 20 rows.
3. Gemma writes one sentence per executed analysis with {{rN.COL}} pointers into that analysis's rows.
4. Code verifies each sentence: grounded numbers and pointers (render_answer), trend words backed by a cited
   change value with the right sign, superlatives citing the first row. Anything else is dropped and counted.
"""
from __future__ import annotations

import re

from .contracts import (Envelope, ExecutionResult, Insight, InsightFindings, InsightProposals, InsightsReport,
                        RenderedAnswer)
from .gate import check_sql
from .gemma import ModelError
from .prompts import MAX_RESULT_ROWS_IN_PROMPT
from .receipt import filter_literals
from .render import render_answer
from .sf_exec import QueryError, SnowflakeUnavailable

MAX_ANALYSES = 3
MAX_INSIGHT_ROWS = 20
UP_WORDS = {"increase", "increased", "increases", "increasing", "rose", "risen", "grew", "growth", "grown", "up", "gained", "higher"}
DOWN_WORDS = {"decrease", "decreased", "decreases", "decreasing", "fell", "fallen", "declined", "decline", "dropped", "drop", "down", "lower"}
SUPERLATIVES = {"largest", "highest", "biggest", "most", "top", "smallest", "lowest", "least", "fewest"}
CHANGE_HINTS = ("CHANGE", "DIFF", "GROWTH", "DELTA", "SLOPE", "TREND")

PROPOSE_SYSTEM = """You suggest follow-up analyses for DataMind, a tool that answers questions from Snowflake data.
Given a question, the SQL that answered it and a preview of its result, propose at most 3 short analyses that help
the user understand the answer: a trend over time, a share of a total, an outlier against peers, or a comparison
between periods or groups. Skip any that would not add information.

Rules for each analysis:
- One read-only Snowflake SELECT using only the listed tables, columns and joins (WITH clauses allowed).
- Compute every number a finding could cite as an explicit column with a clear UPPER_SNAKE_CASE name:
  PCT_CHANGE or DIFF for changes, SHARE_PCT for shares, Z_SCORE or DIFF_FROM_AVG for outliers, RANK for ranks.
  Divide with NULLIF(x, 0). Order so the most notable row comes first. At most 20 rows.
Return JSON: {"analyses": [{"kind": "trend" | "share" | "outlier" | "comparison", "title": "...", "sql": "..."}]}"""

WRITE_SYSTEM = """You write one short finding per analysis for DataMind. Each analysis already ran in Snowflake;
you see its title and its result rows.
Hard rules:
- Refer to every value with a placeholder {{rN.COLUMN}} taken from that analysis's own rows (r0 is its first row).
  Never type numbers yourself.
- State only what the rows show. Use words like "increased" or "decreased" only together with a placeholder for the
  change column that shows it. Use "largest" or "highest" only for r0 when the rows are ordered that way.
- If an analysis shows nothing notable, return an empty finding for it.
Return JSON: {"findings": [{"index": 0, "finding": "..."}]}"""


def dig_deeper(dm, env: Envelope) -> InsightsReport:
    """Run Verified Insights for an answered question. `dm` is a datamind.pipeline.DataMind."""
    report = InsightsReport()
    if env.status != "ANSWER" or env.execution is None or env.plan is None:
        report.message = "Insights need an answered question."
        return report
    allow_cloud = dm.profile.cloud_allowed
    try:
        proposals, call = dm.gemma.generate_json("insights", PROPOSE_SYSTEM, _propose_prompt(dm.profile, env),
                                                 InsightProposals, 1500, allow_cloud=allow_cloud)
    except ModelError as exc:
        report.message = f"Gemma could not propose analyses: {exc}"
        return report
    report.llm_calls.append(call)
    executed: list[tuple[Insight, ExecutionResult]] = []
    for proposal in proposals.analyses[:MAX_ANALYSES]:
        insight = Insight(kind=proposal.kind, title=proposal.title, sql=proposal.sql)
        gate = check_sql(proposal.sql, dm.profile, max_rows=MAX_INSIGHT_ROWS)
        if not gate.ok:
            insight.reason = "rejected by the SQL gate: " + "; ".join(gate.errors[:2])
            report.dropped.append(insight)
            continue
        try:
            problems = dm.runner.explain(gate.sql or "")
            if problems:
                raise QueryError("; ".join(problems))
            result = dm.runner.execute(gate.sql or "", MAX_INSIGHT_ROWS)
        except (QueryError, SnowflakeUnavailable) as exc:
            insight.reason = f"Snowflake could not run it: {exc}"
            report.dropped.append(insight)
            continue
        insight.sql, insight.query_id, insight.columns, insight.rows = result.sql, result.query_id, result.columns, result.rows
        if not result.rows:
            insight.reason = "the analysis returned no rows"
            report.dropped.append(insight)
            continue
        executed.append((insight, result))
    if not executed:
        report.message = report.message or "No proposed analysis could be verified."
        return report
    try:
        findings, call = dm.gemma.generate_json("insights", WRITE_SYSTEM, _write_prompt(env, executed),
                                                InsightFindings, 800, allow_cloud=allow_cloud)
        report.llm_calls.append(call)
        texts = {f.index: f.finding for f in findings.findings}
    except ModelError as exc:
        texts = {}
        report.message = f"Gemma could not word the findings: {exc}"
    money = dm.profile.money_columns()
    for index, (insight, result) in enumerate(executed):
        text = (texts.get(index) or "").strip()
        if not text:
            insight.reason = "no notable finding was written"
            report.dropped.append(insight)
            continue
        verdict = verify_finding(text, insight.title, result, money)
        if isinstance(verdict, str):
            insight.reason = verdict
            report.dropped.append(insight)
        else:
            insight.answer, insight.verified = verdict, True
            report.shown.append(insight)
    return report


def verify_finding(text: str, title: str, result: ExecutionResult, money: set[str]) -> RenderedAnswer | str:
    """The rendered finding if every claim is backed by the analysis's rows, else the reason it is dropped."""
    numbers, dates = filter_literals(result.sql)
    rendered = render_answer(text, result, title, money, audit_numbers=numbers, audit_dates=dates)
    if rendered.fallback_used:
        return "unverified numbers or pointers: " + ", ".join(rendered.rejected_numbers[:3])
    words = set(re.findall(r"[a-z]+", text.lower()))
    cited = [(s.row, s.column) for s in rendered.segments if s.row is not None and s.column]
    signs = []
    for row, column in cited:
        if column and any(h in column.upper() for h in CHANGE_HINTS):
            value = result.rows[row][result.columns.index(column)]
            if isinstance(value, (int, float)) or hasattr(value, "is_signed"):
                signs.append(float(value))
    if words & UP_WORDS or words & DOWN_WORDS:
        if not signs:
            return "trend words without a cited change value"
        if words & UP_WORDS and not words & DOWN_WORDS and any(v <= 0 for v in signs):
            return "says it increased, but the cited change is not positive"
        if words & DOWN_WORDS and not words & UP_WORDS and any(v >= 0 for v in signs):
            return "says it decreased, but the cited change is not negative"
    if words & SUPERLATIVES and cited and any(row != 0 for row, _ in cited[:1]):
        return "superlative claim that does not cite the first row"
    return rendered


def _propose_prompt(profile, env: Envelope) -> str:
    x = env.execution
    assert x is not None and env.plan is not None
    tables = profile.relevant_tables(env.plan.standalone_question or env.question)
    preview = "\n".join(", ".join(f"{c}={v}" for c, v in zip(x.columns, row)) for row in x.rows[:10])
    return (f"Dataset: {profile.title}. {profile.description}\n\n{profile.render_schema(tables)}\n\n"
            f"Question: {env.plan.standalone_question or env.question}\nSQL that answered it:\n{' '.join(x.sql.split())}\n"
            f"Result ({x.row_count} rows; first 10):\n{preview}\n\nPropose at most {MAX_ANALYSES} analyses as JSON.")


def _write_prompt(env: Envelope, executed: list[tuple[Insight, ExecutionResult]]) -> str:
    parts = [f"Original question: {env.question}"]
    for index, (insight, result) in enumerate(executed):
        parts.append(f"\nAnalysis {index} ({insight.kind}): {insight.title}\nColumns: {', '.join(result.columns)}")
        for r, row in enumerate(result.rows[:MAX_RESULT_ROWS_IN_PROMPT]):
            parts.append(f"r{r}: " + ", ".join(f"{c}={v}" for c, v in zip(result.columns, row)))
    parts.append('\nReturn {"findings": [{"index": N, "finding": "..."}]} with placeholders for every value.')
    return "\n".join(parts)
