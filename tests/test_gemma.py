import json

import httpx
import pytest
from pydantic import BaseModel

from datamind.config import Settings
from datamind.gemma import ContextBudgetError, FallbackableError, GemmaClient, ModelError, extract_json

OLLAMA = "http://ollama.test:11434"


class Reply(BaseModel):
    ok: bool
    word: str


def settings(tmp_path, **overrides):
    base = dict(
        snowflake_account="", snowflake_user="", snowflake_role="R", snowflake_warehouse="W",
        snowflake_database="D", snowflake_private_key_path="", snowflake_private_key_passphrase="",
        gemini_api_key="test-key", ollama_host=OLLAMA, ollama_num_ctx=4096, cache_dir=tmp_path,
    )
    base.update(overrides)
    return Settings(**base)


def gemini_ok(text='{"ok": true, "word": "hi"}'):
    return httpx.Response(200, json={
        "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 5},
    })


def ollama_ok(text='{"ok": true, "word": "local"}', prompt_eval=50, eval_count=10):
    return httpx.Response(200, json={"message": {"content": text}, "prompt_eval_count": prompt_eval,
                                     "eval_count": eval_count, "eval_duration": 1_000_000_000})


class Recorder:
    def __init__(self, gemini=None, ollama=None):
        self.gemini, self.ollama, self.requests = list(gemini or []), list(ollama or []), []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        queue = self.gemini if request.url.host == "generativelanguage.googleapis.com" else self.ollama
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def client(tmp_path, recorder, **overrides):
    return GemmaClient(settings(tmp_path, **overrides), transport=httpx.MockTransport(recorder), sleep=lambda s: None)


def test_gemini_primary_success(tmp_path):
    rec = Recorder(gemini=[gemini_ok()])
    result, call = client(tmp_path, rec).generate_json("plan", "SYS", "USER", Reply)
    assert result.word == "hi" and call.tier == "gemini" and call.model == "gemma-4-26b-a4b-it"
    request = rec.requests[0]
    body = json.loads(request.content)
    assert body["systemInstruction"]["parts"][0]["text"] == "SYS"
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert request.headers["x-goog-api-key"] == "test-key" and "test-key" not in str(request.url)


def test_role_model_override(tmp_path):
    rec = Recorder(gemini=[gemini_ok()])
    _, call = client(tmp_path, rec, gemini_role_models={"plan": "gemma-4-31b-it"}).generate_json("plan", "S", "U", Reply)
    assert call.model == "gemma-4-31b-it" and "gemma-4-31b-it" in str(rec.requests[0].url)


def test_system_instruction_unsupported_is_inlined(tmp_path):
    rec = Recorder(gemini=[httpx.Response(400, json={"error": {"message": "Developer instruction is not enabled"}}), gemini_ok()])
    gemma = client(tmp_path, rec)
    gemma.generate_json("plan", "SYS", "USER", Reply)
    retry = json.loads(rec.requests[1].content)
    assert "systemInstruction" not in retry and retry["contents"][0]["parts"][0]["text"].startswith("SYS")
    assert gemma.tiers["gemini"].capabilities("gemma-4-26b-a4b-it")["system_instruction"] is False


def test_json_mode_unsupported_is_dropped(tmp_path):
    rec = Recorder(gemini=[httpx.Response(400, json={"error": {"message": "JSON mode is not enabled for this model"}}), gemini_ok()])
    client(tmp_path, rec).generate_json("plan", "S", "U", Reply)
    assert "responseMimeType" not in json.loads(rec.requests[1].content)["generationConfig"]


def test_rate_limit_falls_back_to_ollama(tmp_path):
    limited = httpx.Response(429, json={"error": {"message": "quota", "details": [
        {"violations": [{"quotaMetric": "generate_requests_per_minute", "quotaValue": "30"}]}]}})
    rec = Recorder(gemini=[limited, limited, limited], ollama=[ollama_ok()])
    result, call = client(tmp_path, rec).generate_json("plan", "S", "U", Reply)
    assert result.word == "local" and call.tier == "ollama" and call.fallback_used


def test_rate_limit_without_backup_raises(tmp_path):
    limited = httpx.Response(429, json={"error": {"message": "quota"}})
    rec = Recorder(gemini=[limited, limited, limited])
    with pytest.raises(FallbackableError, match="429"):
        client(tmp_path, rec, gemma_fallback="none").generate_json("plan", "S", "U", Reply)


def test_timeout_falls_back(tmp_path):
    rec = Recorder(gemini=[httpx.ReadTimeout("slow")], ollama=[ollama_ok()])
    _, call = client(tmp_path, rec).generate_json("plan", "S", "U", Reply)
    assert call.tier == "ollama"


def test_bad_request_does_not_fall_back(tmp_path):
    rec = Recorder(gemini=[httpx.Response(400, json={"error": {"message": "model not found"}})], ollama=[ollama_ok()])
    with pytest.raises(ModelError, match="model not found"):
        client(tmp_path, rec).generate_json("plan", "S", "U", Reply)
    assert len(rec.ollama) == 1  # backup untouched


def test_invalid_output_retried_then_rejected(tmp_path):
    rec = Recorder(gemini=[gemini_ok("not json"), gemini_ok('{"ok": true, "word": "fixed"}')])
    result, _ = client(tmp_path, rec).generate_json("plan", "S", "U", Reply)
    assert result.word == "fixed" and "previous reply was invalid" in json.loads(rec.requests[1].content)["contents"][0]["parts"][0]["text"]
    rec2 = Recorder(gemini=[gemini_ok('{"ok": "maybe"}'), gemini_ok("{}")])
    with pytest.raises(ModelError, match="Reply contract"):
        client(tmp_path / "b", rec2).generate_json("plan", "S", "U2", Reply)


def test_ollama_budget_picks_smaller_prompt_level(tmp_path):
    rec = Recorder(ollama=[ollama_ok()])
    gemma = client(tmp_path, rec, gemini_api_key="", gemma_provider="ollama", gemma_fallback="none", ollama_num_ctx=1024)
    big, small = "x" * 6000, "short prompt"
    _, call = gemma.generate_json("plan", "S", [big, small], Reply, max_output_tokens=200)
    assert json.loads(rec.requests[0].content)["messages"][1]["content"] == small
    assert json.loads(rec.requests[0].content)["options"]["num_ctx"] == 1024


def test_ollama_budget_fails_loudly(tmp_path):
    gemma = client(tmp_path, Recorder(), gemini_api_key="", gemma_provider="ollama", gemma_fallback="none", ollama_num_ctx=1024)
    with pytest.raises(ContextBudgetError):
        gemma.generate_json("plan", "S", "y" * 6000, Reply, max_output_tokens=200)


def test_ollama_full_context_is_rejected(tmp_path):
    rec = Recorder(ollama=[ollama_ok(prompt_eval=4000, eval_count=96)])
    gemma = client(tmp_path, rec, gemini_api_key="", gemma_provider="ollama", gemma_fallback="none")
    with pytest.raises(ModelError, match="context window full"):
        gemma.generate_json("plan", "S", "U", Reply)


def test_cache_hit_skips_network(tmp_path):
    rec = Recorder(gemini=[gemini_ok()])
    gemma = client(tmp_path, rec)
    gemma.generate_json("plan", "S", "same prompt", Reply)
    result, call = gemma.generate_json("plan", "S", "same prompt", Reply)
    assert call.cached and result.word == "hi" and len(rec.requests) == 1


def test_no_tier_configured(tmp_path):
    with pytest.raises(ModelError, match="no Gemma tier"):
        client(tmp_path, Recorder(), gemini_api_key="", ollama_host="").generate_json("plan", "S", "U", Reply)


def test_thinking_budget_per_role(tmp_path):
    rec = Recorder(gemini=[gemini_ok(), gemini_ok('{"ok": true, "word": "b"}')])
    gemma = client(tmp_path, rec, gemini_thinking={"plan": "default", "explain": "minimal"})
    gemma.generate_json("plan", "S", "U1", Reply, max_output_tokens=700)
    gemma.generate_json("explain", "S", "U2", Reply, max_output_tokens=300)
    plan_cfg = json.loads(rec.requests[0].content)["generationConfig"]
    explain_cfg = json.loads(rec.requests[1].content)["generationConfig"]
    assert "thinkingConfig" not in plan_cfg and plan_cfg["maxOutputTokens"] == 700 + 3072
    assert explain_cfg["thinkingConfig"] == {"thinkingLevel": "minimal"} and explain_cfg["maxOutputTokens"] == 300


def test_thinking_level_unsupported_is_dropped(tmp_path):
    rec = Recorder(gemini=[httpx.Response(400, json={"error": {"message": "Thinking level is not supported for this model."}}),
                           gemini_ok()])
    gemma = client(tmp_path, rec)
    gemma.generate_json("explain", "S", "U", Reply, max_output_tokens=300)
    retry = json.loads(rec.requests[1].content)["generationConfig"]
    assert "thinkingConfig" not in retry and retry["maxOutputTokens"] == 300 + 3072


def test_only_thought_parts_is_an_error(tmp_path):
    thought_only = httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "hmm", "thought": True}]},
                                                             "finishReason": "MAX_TOKENS"}], "usageMetadata": {}})
    with pytest.raises(ModelError, match="no visible text"):
        client(tmp_path, Recorder(gemini=[thought_only])).generate_json("plan", "S", "U", Reply)


def test_ollama_asks_for_no_thinking_and_drops_flag_if_rejected(tmp_path):
    rec = Recorder(ollama=[httpx.Response(400, text='{"error":"model does not support think"}'), ollama_ok()])
    gemma = client(tmp_path, rec, gemini_api_key="", gemma_provider="ollama", gemma_fallback="none")
    gemma.generate_json("plan", "S", "U", Reply)
    assert json.loads(rec.requests[0].content)["think"] is False
    assert "think" not in json.loads(rec.requests[1].content)


def test_extract_json_tolerates_fences():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(ValueError):
        extract_json("no object here")
