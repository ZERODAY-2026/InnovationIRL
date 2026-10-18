"""Command line: python -m datamind ask "Which 5 nations had the highest revenue in 1995?" [--profile tpch] [--json]"""
from __future__ import annotations

import argparse
import sys

from .config import load_settings
from .contracts import Envelope
from .pipeline import DataMind
from .profile import load_profile
from .render import format_value

MAX_PRINTED_ROWS = 10


def print_envelope(env: Envelope, money: set[str]) -> None:
    print(f"Status: {env.status}")
    if env.answer:
        flag = "deterministic fallback" if env.answer.fallback_used else "Gemma text, values from cells"
        print(f"Answer: {env.answer.text}   [{flag}]")
        if env.answer.rejected_numbers:
            print(f"  (model text rejected: {', '.join(env.answer.rejected_numbers)})")
    if env.message:
        print(f"Message: {env.message}")
    if env.plan and env.plan.options:
        for option in env.plan.options:
            print(f"  - {option}")
    if env.plan and env.plan.standalone_question and env.plan.standalone_question != env.question:
        print(f"Interpreted as: {env.plan.standalone_question}")
    if env.plan and env.plan.assumptions:
        print("Assumptions: " + " | ".join(env.plan.assumptions))
    if env.receipt:
        r = env.receipt
        print("How it was computed:")
        for label, items in (("tables", r.tables), ("joins", r.joins), ("filters", r.filters),
                             ("grouped by", r.group_by), ("measures", r.measures), ("sorted by", r.order_by)):
            if items:
                print(f"  {label}: " + "; ".join(items))
        if r.limit:
            print(f"  limited to: {r.limit} rows")
    if env.execution:
        x = env.execution
        print(f"Snowflake query ID: {x.query_id} | role {x.role} | warehouse {x.warehouse} | {x.elapsed_ms} ms | "
              f"{x.row_count} rows{' (truncated)' if x.truncated else ''}")
        print("SQL:\n  " + x.sql.replace("\n", "\n  "))
        print("Rows:")
        print("  " + " | ".join(x.columns))
        for row in x.rows[:MAX_PRINTED_ROWS]:
            print("  " + " | ".join(format_value(v, c, money) for c, v in zip(x.columns, row)))
        if x.row_count > MAX_PRINTED_ROWS:
            print(f"  ... {x.row_count - MAX_PRINTED_ROWS} more rows")
    elif env.gate and env.gate.errors:
        print("Gate: " + "; ".join(env.gate.errors[:3]))
    for call in env.llm_calls:
        print(f"Gemma {call.role}: {call.tier}/{call.model} {call.latency_ms} ms"
              f"{' (cached)' if call.cached else ''}{' (fallback)' if call.fallback_used else ''}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m datamind")
    sub = parser.add_subparsers(dest="command", required=True)
    ask = sub.add_parser("ask", help="answer one question")
    ask.add_argument("question")
    ask.add_argument("--profile")
    ask.add_argument("--json", action="store_true", help="print the full evidence envelope as JSON")
    args = parser.parse_args(argv)
    settings = load_settings()
    profile = load_profile(args.profile or settings.profile)
    env = DataMind(settings, profile).ask(args.question)
    if args.json:
        print(env.model_dump_json(indent=2))
    else:
        print_envelope(env, profile.money_columns())
    return 0 if env.status != "ERROR" else 1


if __name__ == "__main__":
    sys.exit(main())
