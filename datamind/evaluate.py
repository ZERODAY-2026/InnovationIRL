"""Evaluate the live pipeline on a question set against independent, hand-written gold SQL.

python -m datamind.evaluate eval/dev_pipeline.yaml [--label NAME] [--plan-model ID] [--plan-thinking default|minimal]
                            [--answers model|deterministic] [--only d01,d02]

Both the gold SQL and the pipeline's SQL run in Snowflake; results are compared by value, tolerating
column order, extra columns, legitimate rounding and percent-vs-fraction. Writes
eval/results/<UTC>_<set>_<label>.json with SQL, query IDs and verdicts only (no data rows).
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import statistics
import sys
import time
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from .config import ROOT, load_settings
from .contracts import Envelope, ExecutionResult
from .pipeline import DataMind
from .profile import load_profile
from .prompts import Turn
from .render import is_empty

RESULTS_DIR = ROOT / "eval" / "results"
REFUSED = {"UNSAFE", "UNSUPPORTED"}
RATE_LIMIT_PAUSE_S = 30


# ---------------------------------------------------------------------------------------------- comparison
def _num(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    if isinstance(value, int):
        return Decimal(value)
    return None


def _decimals(d: Decimal) -> int:
    exponent = d.normalize().as_tuple().exponent
    return max(0, -exponent) if isinstance(exponent, int) else 0


def numbers_match(a: Decimal, b: Decimal) -> bool:
    """Integers must be equal. Decimals may differ by Snowflake's 6-decimal division noise or 1e-9 relative,
    or be a legitimate rounding of the other (at least 2 decimals or 4 significant digits kept)."""
    if a == b:
        return True
    if a == a.to_integral_value() and b == b.to_integral_value():
        return False
    if abs(a - b) <= Decimal("0.0000015") or abs(a - b) <= Decimal("1e-9") * max(abs(a), abs(b)):
        return True
    for x, y in ((a, b), (b, a)):
        places = _decimals(x)
        significant = len(x.normalize().as_tuple().digits)
        if places <= 4 and (places >= 2 or significant >= 4):
            try:
                if y.quantize(Decimal(1).scaleb(-places)) == x:
                    return True
            except InvalidOperation:
                pass
    return False


def _cell_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    return "" if value is None else str(value).strip()


def cells_match(system: Any, gold: Any, scale: Decimal = Decimal(1)) -> bool:
    s, g = _num(system), _num(gold)
    if s is not None and g is not None:
        return numbers_match(s, g * scale)
    if s is None and g is None:
        return _cell_text(system) == _cell_text(gold)
    return False


def _sort_key(value: Any) -> tuple[int, Any]:
    number = _num(value)
    return (0, float(number)) if number is not None else (1, _cell_text(value))


def _column_matches(system_values: list[Any], gold_values: list[Any], scale: Decimal) -> bool:
    return all(cells_match(s, g, scale) for s, g in zip(sorted(system_values, key=_sort_key),
                                                         sorted(gold_values, key=lambda v: _sort_key(_scaled(v, scale)))))


def _scaled(value: Any, scale: Decimal) -> Any:
    number = _num(value)
    return number * scale if number is not None else value


def compare_results(system_columns: list[str], system_rows: list[list[Any]], gold_columns: list[str],
                    gold_rows: list[list[Any]], order_matters: bool = False,
                    match: list[int] | None = None) -> tuple[bool, str]:
    """True when every required gold column is reproduced by a distinct system column, row for row."""
    if len(system_rows) != len(gold_rows):
        return False, f"row count {len(system_rows)} != gold {len(gold_rows)}"
    if not gold_rows:
        return True, "both empty"
    required = match if match is not None else list(range(len(gold_columns)))
    candidates: dict[int, list[tuple[int, Decimal]]] = {}
    for g in required:
        gold_values = [row[g] for row in gold_rows]
        options = []
        for s in range(len(system_columns)):
            system_values = [row[s] for row in system_rows]
            for scale in (Decimal(1), Decimal(100), Decimal("0.01")):
                if _column_matches(system_values, gold_values, scale):
                    options.append((s, scale))
                    break
        if not options:
            return False, f"no system column reproduces gold column {gold_columns[g]}"
        candidates[g] = options

    def rows_match(mapping: dict[int, tuple[int, Decimal]]) -> bool:
        projected = [[row[mapping[g][0]] for g in required] for row in system_rows]
        expected = [[_scaled(row[g], mapping[g][1]) for g in required] for row in gold_rows]
        if not order_matters:
            projected = sorted(projected, key=lambda r: [_sort_key(v) for v in r])
            expected = sorted(expected, key=lambda r: [_sort_key(v) for v in r])
        return all(cells_match(s, g) for ps, es in zip(projected, expected) for s, g in zip(ps, es))

    def assign(index: int, used: set[int], mapping: dict[int, tuple[int, Decimal]]) -> bool:
        if index == len(required):
            return rows_match(mapping)
        g = required[index]
        for s, scale in candidates[g]:
            if s not in used and assign(index + 1, used | {s}, {**mapping, g: (s, scale)}):
                return True
        return False

    if assign(0, set(), {}):
        return True, "match"
    return False, "columns match individually but rows do not line up" + (" in order" if order_matters else "")


# ---------------------------------------------------------------------------------------------- scoring
def score(item: dict[str, Any], env: Envelope, gold: tuple[list[str], list[list[Any]]] | None) -> tuple[bool, str]:
    expect = item.get("expect", "answer")
    accept = set(item.get("accept", []))
    status = env.status
    if expect == "refuse":
        ok = status in REFUSED and env.execution is None
        return ok, f"status {status}" + ("" if env.execution is None else " and a query ran")
    if expect == "unsupported":
        ok = (status == "UNSUPPORTED" or status in accept) and env.execution is None
        return ok, f"status {status}"
    if status in accept and status != "ANSWER":
        return True, f"accepted status {status}"
    if status != "ANSWER" or env.execution is None:
        return False, f"status {status}: {env.message or ''}"[:160]
    if expect == "empty":
        return is_empty(env.execution), "empty result" if is_empty(env.execution) else "expected no rows"
    assert gold is not None
    gold_columns, gold_rows = gold
    return compare_results(env.execution.columns, env.execution.rows, gold_columns, gold_rows,
                           order_matters=bool(item.get("order_matters", False)), match=item.get("match"))


def _turn(env: Envelope) -> Turn:
    x: ExecutionResult | None = env.execution
    return Turn(question=env.plan.standalone_question if env.plan else env.question, sql=x.sql if x else None,
                columns=x.columns if x else [], row_count=x.row_count if x else 0)


def run(set_path: Path, label: str, plan_model: str | None, plan_thinking: str | None, model_answers: bool,
        only: set[str] | None) -> dict[str, Any]:
    spec = yaml.safe_load(set_path.read_text(encoding="utf-8"))
    settings = load_settings()
    if plan_model:
        settings = dataclasses.replace(settings, gemini_role_models={**settings.gemini_role_models, "plan": plan_model})
    if plan_thinking:
        settings = dataclasses.replace(settings, gemini_thinking={**settings.gemini_thinking, "plan": plan_thinking})
    profile = load_profile(spec.get("profile", settings.profile))
    dm = DataMind(settings, profile)
    envelopes: dict[str, Envelope] = {}
    records = []
    started = datetime.now(timezone.utc)
    for item in spec["questions"]:
        if only and item["id"] not in only:
            continue
        gold = None
        gold_qid = None
        if item.get("gold_sql"):
            gold_qid, columns, rows = dm.runner.run_trusted(item["gold_sql"])
            gold = (columns, [list(r) for r in rows])
        history = [_turn(envelopes[item["follows"]])] if item.get("follows") in envelopes else None
        env = _ask_with_pause(dm, item["question"], history, model_answers)
        envelopes[item["id"]] = env
        passed, reason = score(item, env, gold)
        plan_calls = [c for c in env.llm_calls if c.role == "plan"]
        records.append({
            "id": item["id"], "category": item.get("category", ""), "expect": item.get("expect", "answer"),
            "question": item["question"], "status": env.status, "passed": passed, "reason": reason,
            "query_id": env.execution.query_id if env.execution else None, "gold_query_id": gold_qid,
            "sql": env.execution.sql if env.execution else (env.plan.sql if env.plan else None),
            "assumptions": env.plan.assumptions if env.plan else [],
            "plan_ms": sum(c.latency_ms for c in plan_calls), "repairs": max(0, len(plan_calls) - 1),
            "plan_cached": all(c.cached for c in plan_calls) if plan_calls else False,
            "tiers": sorted({f"{c.tier}/{c.model}" for c in env.llm_calls}),
            "fallback_tier_used": any(c.fallback_used for c in env.llm_calls),
            "answer_grounded": env.answer.grounded if env.answer else None,
            "answer_fallback": env.answer.fallback_used if env.answer else None,
            "rejected_numbers": env.answer.rejected_numbers if env.answer else [],
        })
        mark = "PASS" if passed else "FAIL"
        print(f"{item['id']} {mark} {env.status:<11} {records[-1]['plan_ms']:>6} ms  {item['question'][:60]}"
              + ("" if passed else f"  -> {reason}"), flush=True)
    dm.runner.close()
    return summarize(spec.get("name", set_path.stem), label, settings, started, records, model_answers)


def _ask_with_pause(dm: DataMind, question: str, history, model_answers: bool) -> Envelope:
    for attempt in range(3):
        env = dm.ask(question, history, model_answer=model_answers)
        if env.status == "ERROR" and env.message and "429" in env.message and attempt < 2:
            print(f"   rate limited; pausing {RATE_LIMIT_PAUSE_S} s", flush=True)
            time.sleep(RATE_LIMIT_PAUSE_S)
            continue
        return env
    return env


def summarize(name: str, label: str, settings, started: datetime, records: list[dict], model_answers: bool) -> dict:
    def rate(rows: list[dict]) -> str:
        return f"{sum(r['passed'] for r in rows)}/{len(rows)}" if rows else "0/0"

    answers = [r for r in records if r["expect"] in ("answer", "empty")]
    refusals = [r for r in records if r["expect"] in ("refuse", "unsupported")]
    live = [r["plan_ms"] for r in records if not r["plan_cached"] and r["plan_ms"]]
    categories = sorted({r["category"] for r in records})
    summary = {
        "set": name, "label": label, "started_at": started.isoformat(timespec="seconds"),
        "plan_model": settings.gemini_model_for("plan"), "plan_thinking": settings.gemini_thinking_for("plan"),
        "answers_by_model": model_answers,
        "totals": {
            "overall": rate(records), "answers": rate(answers), "refusals": rate(refusals),
            "by_category": {c: rate([r for r in records if r["category"] == c]) for c in categories},
            "plan_ms_median": int(statistics.median(live)) if live else None,
            "plan_ms_max": max(live) if live else None,
            "repairs": sum(r["repairs"] for r in records),
            "fallback_tier_used": sum(r["fallback_tier_used"] for r in records),
            "answers_with_model_text_rejected": sum(1 for r in records if r["answer_fallback"] and r["rejected_numbers"]),
        },
        "questions": records,
    }
    return summary


def report_table(paths: list[Path]) -> str:
    """Markdown table of saved runs, so documentation quotes results instead of retyping them."""
    lines = ["| Run | Started (UTC) | PLAN model | Thinking | Wording | Overall | Answers | Refusals | Plan median | Repairs | Failed |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for path in paths:
        run = json.loads(path.read_text(encoding="utf-8"))
        t = run["totals"]
        failed = "; ".join(f"{q['id']}: {q['reason'][:60]}" for q in run["questions"] if not q["passed"]) or "none"
        median = f"{t['plan_ms_median'] / 1000:.1f} s" if t.get("plan_ms_median") else "cached"
        lines.append(f"| {run['label']} | {run['started_at'][:16]} | {run['plan_model']} | {run['plan_thinking']} | "
                     f"{'Gemma' if run.get('answers_by_model') else 'code'} | {t['overall']} | {t['answers']} | "
                     f"{t['refusals']} | {median} | {t['repairs']} | {failed} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    if argv is None and len(sys.argv) > 1 and sys.argv[1] == "--report":
        print(report_table(sorted(RESULTS_DIR.glob("*_dev_pipeline_*.json"))))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("set_path", type=Path)
    parser.add_argument("--label", default="default")
    parser.add_argument("--plan-model")
    parser.add_argument("--plan-thinking", choices=["default", "minimal"])
    parser.add_argument("--answers", choices=["model", "deterministic"], default="deterministic",
                        help="'model' also runs the Gemma wording step (2 calls per question)")
    parser.add_argument("--only", help="comma-separated question ids")
    args = parser.parse_args(argv)
    only = set(args.only.split(",")) if args.only else None
    summary = run(args.set_path, args.label, args.plan_model, args.plan_thinking, args.answers == "model", only)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = RESULTS_DIR / f"{stamp}_{summary['set']}_{args.label}.json"
    out.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    t = summary["totals"]
    print(f"\n{summary['set']} [{args.label}] plan={summary['plan_model']} thinking={summary['plan_thinking']}: "
          f"overall {t['overall']}, answers {t['answers']}, refusals {t['refusals']}, "
          f"plan median {t['plan_ms_median']} ms, repairs {t['repairs']}")
    print("by category: " + ", ".join(f"{k} {v}" for k, v in t["by_category"].items()))
    print(f"results: {out.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
