"""Question -> Gemma plan -> gate -> Snowflake EXPLAIN -> Snowflake execution -> cell-bound answer + receipt."""
from __future__ import annotations

import time

from .config import Settings, load_settings
from .contracts import AnswerDraft, Envelope, Plan, RenderedAnswer, Segment
from .gate import check_sql
from .gemma import GemmaClient, ModelError
from .profile import Profile, load_profile
from .prompts import EXPLAIN_SYSTEM, PLAN_SYSTEM, Turn, explain_prompt, plan_prompts
from .receipt import build_receipt, filter_literals, period_filters, zero_row_probe
from .render import deterministic_answer, is_empty, render_answer
from .sf_exec import QueryError, SnowflakeRunner, SnowflakeUnavailable

MAX_REPAIRS = 2
PLAN_MAX_TOKENS = 700
EXPLAIN_MAX_TOKENS = 300


class DataMind:
    def __init__(self, settings: Settings | None = None, profile: Profile | None = None,
                 gemma: GemmaClient | None = None, runner: SnowflakeRunner | None = None):
        self.settings = settings or load_settings()
        self.profile = profile or load_profile(self.settings.profile)
        self.gemma = gemma or GemmaClient(self.settings)
        self.runner = runner or SnowflakeRunner(self.settings, self.profile.schema)

    def ask(self, question: str, history: list[Turn] | None = None, model_answer: bool = True) -> Envelope:
        """Answer one question. model_answer=False skips the Gemma wording step (deterministic answer)."""
        env = Envelope(question=question, profile=self.profile.name, status="ERROR")
        question = question.strip()
        if not question:
            env.message = "Please type a question about the data."
            return env
        allow_cloud = self.profile.cloud_allowed
        repair: tuple[str, list[str]] | None = None
        gate = None
        for _ in range(MAX_REPAIRS + 1):
            started = time.perf_counter()
            try:
                plan, call = self.gemma.generate_json("plan", PLAN_SYSTEM, plan_prompts(self.profile, question, history, repair),
                                                      Plan, PLAN_MAX_TOKENS, allow_cloud=allow_cloud)
            except ModelError as exc:
                env.message = f"Gemma could not plan this question: {exc}"
                return env
            _add_time(env, "plan_ms", started)
            env.llm_calls.append(call)
            env.plan = plan
            if plan.status != "ANSWER":
                env.status = plan.status
                env.message = _status_message(plan)
                return env
            started = time.perf_counter()
            gate = check_sql(plan.sql, self.profile)
            env.gate = gate
            _add_time(env, "gate_ms", started)
            if not gate.ok:
                repair = (plan.sql or "", gate.errors)
                continue
            started = time.perf_counter()
            try:
                problems = self.runner.explain(gate.sql or "")
            except SnowflakeUnavailable as exc:
                env.message = str(exc)
                return env
            except QueryError as exc:
                problems = [f"Snowflake could not compile the query: {exc}"]
            _add_time(env, "explain_ms", started)
            if problems:
                env.gate = gate.model_copy(update={"ok": False, "errors": problems})
                repair = (plan.sql or "", problems)
                gate = None
                continue
            break
        if gate is None or not gate.ok:
            last = env.gate.errors if env.gate else []
            env.message = ("I could not write a valid read-only query for this question after "
                           f"{MAX_REPAIRS + 1} attempts. Last problems: " + "; ".join(last[:3]))
            return env

        started = time.perf_counter()
        try:
            result = self.runner.execute(gate.sql or "", gate.row_cap or self.profile.max_rows)
        except (QueryError, SnowflakeUnavailable) as exc:
            env.message = f"Snowflake could not run the query: {exc}"
            return env
        _add_time(env, "execute_ms", started)
        env.execution = result
        env.receipt = build_receipt(result.sql, self.profile, env.plan.assumptions if env.plan else [], gate.row_cap)
        env.status = "ANSWER"
        env.answer = self._answer(question, env, allow_cloud, model_answer)
        note = self._outside_coverage_note(result.sql)
        if note and env.answer and note not in env.answer.text:
            env.answer.segments.append(Segment(text=" " + note))
        if result.row_count == 0 and env.answer:
            self._diagnose_empty(env, result.sql)
        return env

    def _diagnose_empty(self, env: Envelope, sql: str) -> None:
        """Run one Snowflake query that shows which filter removed every row (evidence, not a guess)."""
        try:
            probe = zero_row_probe(sql)
        except Exception:
            probe = None
        if not probe:
            return
        probe_sql, labels = probe
        report = check_sql(probe_sql, self.profile)
        if not report.ok:
            return
        try:
            diag = self.runner.execute(report.sql or "", 1)
        except (QueryError, SnowflakeUnavailable):
            return
        if not diag.rows:
            return
        base, counts = diag.rows[0][0], diag.rows[0][1:]
        env.diagnostic_query_id = diag.query_id
        env.diagnostics = [f"Rows before filters: {int(base or 0):,}"] + [
            f"{label} alone keeps {int(count or 0):,} rows" for label, count in zip(labels, counts)]
        empty = [label for label, count in zip(labels, counts) if not count]
        if empty:
            text = f" The filter {empty[0]} matches no rows in the data (diagnosis query {diag.query_id})."
        else:
            text = f" Each filter matches some rows, but no row satisfies all of them together (diagnosis query {diag.query_id})."
        env.answer.segments.append(Segment(text=text))

    def _outside_coverage_note(self, sql: str) -> str:
        """Code-written note when the query asks about periods the data does not cover (honest missing data)."""
        anchor = self.profile.time_anchor
        if not anchor or anchor not in self.profile.coverage:
            return ""
        lo, hi = self.profile.coverage[anchor]
        try:
            dates, years = period_filters(sql)
        except Exception:
            return ""
        outside = [d for d in dates if d < lo or d > hi] + [str(y) for y in years if y < int(lo[:4]) or y > int(hi[:4])]
        if not outside:
            return ""
        return f"Note: the data only covers {anchor} from {lo} to {hi}, so {', '.join(sorted(set(outside)))} is outside it."

    def _answer(self, question: str, env: Envelope, allow_cloud: bool, model_answer: bool = True) -> RenderedAnswer:
        result = env.execution
        assert result is not None and env.plan is not None
        money = self.profile.money_columns()
        if not model_answer and not is_empty(result):
            return deterministic_answer(result, money)
        if is_empty(result):
            answer = deterministic_answer(result, money)
            return answer
        started = time.perf_counter()
        try:
            draft, call = self.gemma.generate_json(
                "explain", EXPLAIN_SYSTEM,
                explain_prompt(question, env.plan.standalone_question, env.plan.assumptions, result),
                AnswerDraft, EXPLAIN_MAX_TOKENS, allow_cloud=allow_cloud,
            )
        except ModelError:
            return deterministic_answer(result, money, rejected=["explain step unavailable"])
        env.llm_calls.append(call)
        _add_time(env, "explain_answer_ms", started)
        numbers, dates = filter_literals(result.sql)
        coverage_dates = {d for pair in self.profile.coverage.values() for d in pair}
        return render_answer(draft.answer, result, question, money, audit_numbers=numbers,
                             audit_dates=dates | coverage_dates)

def _status_message(plan: Plan) -> str:
    if plan.status == "CLARIFY":
        return plan.clarification or "Could you say a bit more about what you want to know?"
    if plan.status == "UNSUPPORTED":
        return "This dataset cannot answer that. " + (plan.reason or "")
    return "DataMind only reads data and cannot do that. " + (plan.reason or "")


def _add_time(env: Envelope, key: str, started: float) -> None:
    env.timings_ms[key] = env.timings_ms.get(key, 0) + int((time.perf_counter() - started) * 1000)
