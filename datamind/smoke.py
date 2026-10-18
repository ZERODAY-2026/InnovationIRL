"""Smoke test: one call per Gemini model ID, Ollama only if OLLAMA_HOST is set, Snowflake if configured.

Run: python -m datamind.smoke [--models gemma-4-26b-a4b-it gemma-4-31b-it] [--skip-snowflake]
Prints latency, capability and permission results. Never prints keys or .env contents.
"""
from __future__ import annotations

import argparse
import sys
import time

import httpx
from pydantic import BaseModel

from .config import load_settings
from .gemma import FallbackableError, GeminiTier, ModelError, OllamaTier, extract_json

SYSTEM = "You are a JSON API. Reply with one JSON object and nothing else."
USER = 'Return exactly this JSON object: {"ok": true, "word": "datamind"}'


class Echo(BaseModel):
    ok: bool
    word: str


def _valid(text: str) -> str:
    try:
        return "yes" if Echo.model_validate(extract_json(text)).word == "datamind" else "wrong content"
    except Exception:
        return "NO"


def check_gemini(settings, models: list[str]) -> bool:
    if not settings.gemini_ready:
        print("Gemini: SKIPPED (GEMINI_API_KEY not set in .env)")
        return False
    tier = GeminiTier(settings.gemini_api_key, settings.cache_dir / "gemini_caps.json")
    all_ok = True
    for model in models:
        t0 = time.perf_counter()
        try:
            raw = tier.generate(model, SYSTEM, USER, 64)
        except FallbackableError as exc:
            print(f"Gemini {model}: UNAVAILABLE ({exc})")
            all_ok = False
            continue
        except ModelError as exc:
            print(f"Gemini {model}: ERROR ({exc})")
            all_ok = False
            continue
        ms = int((time.perf_counter() - t0) * 1000)
        caps = tier.capabilities(model)
        valid = _valid(raw.text)
        all_ok &= valid == "yes"
        print(
            f"Gemini {model}: OK {ms} ms (incl. capability retries) | system instruction: "
            f"{'yes' if caps['system_instruction'] else 'no (inlined)'} | JSON mode: "
            f"{'yes' if caps['json_mode'] else 'no (pydantic only)'} | schema-valid JSON: {valid} | "
            f"tokens in/out: {raw.prompt_tokens}/{raw.output_tokens}"
        )
    print("Gemini quota: the API reports limits only on a 429; see the AI Studio rate-limit page for the account's quota.")
    return all_ok


def check_ollama(settings) -> bool:
    if not settings.ollama_ready:
        print("Ollama: SKIPPED (OLLAMA_HOST not set)")
        return True
    tier = OllamaTier(settings.ollama_host, settings.ollama_model, settings.ollama_num_ctx)
    try:
        raw = tier.generate(SYSTEM, USER, 64, Echo.model_json_schema())
    except ModelError as exc:
        print(f"Ollama {settings.ollama_model} at OLLAMA_HOST: ERROR ({exc})")
        return False
    split = "unknown"
    try:
        for m in httpx.get(f"{settings.ollama_host}/api/ps", timeout=10).json().get("models", []):
            if m.get("name", "").startswith(settings.ollama_model.split(":")[0]):
                size, vram = m.get("size") or 0, m.get("size_vram") or 0
                split = f"{round(100 * vram / size)}% GPU / {round(100 * (size - vram) / size)}% CPU" if size else "unknown"
    except (httpx.HTTPError, ValueError):
        pass
    print(f"Ollama {settings.ollama_model}: {raw.note or '?'} | num_ctx {settings.ollama_num_ctx} | "
          f"split {split} | schema-valid JSON: {_valid(raw.text)}")
    return True


def check_snowflake(settings) -> bool:
    from .profile import load_profile
    from .sf_exec import QueryError, SnowflakeRunner, SnowflakeUnavailable

    if not settings.snowflake_ready:
        print("Snowflake: SKIPPED (SNOWFLAKE_ACCOUNT / SNOWFLAKE_PRIVATE_KEY_PATH not set)")
        return False
    profile = load_profile(settings.profile)
    runner = SnowflakeRunner(settings, profile.schema, tag="datamind:smoke")
    ok = True
    try:
        runner.connect()
        qid, rows = runner.run_unchecked(f"SELECT COUNT(*) FROM {profile.database}.{profile.schema}.{next(iter(profile.tables))}")
        print(f"Snowflake: role={runner.role} warehouse={runner.warehouse} | count query OK ({rows[0][0]} rows) query_id={qid}")
        for label, sql in (
            ("write as app role", f"DELETE FROM {profile.database}.{profile.schema}.{next(iter(profile.tables))} WHERE 1 = 0"),
            ("read sample database", "SELECT 1 FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.REGION LIMIT 1"),
        ):
            try:
                runner.run_unchecked(sql)
                print(f"Snowflake: {label}: ALLOWED (FAIL: the app role must not be able to do this)")
                ok = False
            except QueryError as exc:
                print(f"Snowflake: {label}: denied (PASS) - {str(exc)[:90]}")
    except (SnowflakeUnavailable, QueryError) as exc:
        print(f"Snowflake: ERROR ({exc})")
        ok = False
    finally:
        runner.close()
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs="+", default=["gemma-4-26b-a4b-it", "gemma-4-31b-it"])
    parser.add_argument("--skip-snowflake", action="store_true")
    args = parser.parse_args(argv)
    settings = load_settings()
    results = [check_gemini(settings, args.models), check_ollama(settings)]
    if not args.skip_snowflake:
        results.append(check_snowflake(settings))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
