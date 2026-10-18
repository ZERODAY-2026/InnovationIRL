"""Gemma client: Gemini API tier (primary) and Ollama tier (backup), with JSON outputs validated by pydantic.

- Fallback to the backup tier happens only on a 429, a timeout or a connection error.
- Local prompts are budgeted before the call and checked after it; Ollama never truncates silently.
- Validated replies are cached by prompt hash in .cache/llm (plans only; data is never cached here).
- At most GEMMA_MAX_CONCURRENCY (1-2) calls run at once.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from .config import Settings
from .contracts import LLMCall

T = TypeVar("T", bound=BaseModel)
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
PROMPT_OVERHEAD_TOKENS = 32
# Gemma 4 on the Gemini API thinks by default and thought tokens count against maxOutputTokens.
THINKING_ALLOWANCE_TOKENS = 3072
_DEFAULT_CAPS = {"system_instruction": True, "json_mode": True, "thinking_level": True}


class ModelError(RuntimeError):
    """A model call failed; the message is safe to show (it never contains keys)."""


class FallbackableError(ModelError):
    """429, timeout or connection error: the backup tier may be tried."""


class ContextBudgetError(ModelError):
    """The prompt does not fit the local context window, even at its smallest level."""


def estimate_tokens(text: str) -> int:
    """Conservative token estimate (about 3 characters per token for schema-heavy text)."""
    return math.ceil(len(text) / 3)


def extract_json(text: str) -> dict[str, Any]:
    """Parse the first JSON object in a reply, tolerating code fences and surrounding prose."""
    t = text.strip()
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in the reply")
    return json.loads(t[start : end + 1])


@dataclass
class RawReply:
    text: str
    model: str
    tier: str
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    note: str | None = None


class GeminiTier:
    name = "gemini"

    def __init__(self, api_key: str, caps_file: Path, timeout_s: float = 60.0,
                 transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep):
        self._key = api_key
        self._client = httpx.Client(timeout=timeout_s, transport=transport)
        self._caps_file = caps_file
        self._caps: dict[str, dict[str, bool]] = self._load_caps()
        self._sleep = sleep

    def capabilities(self, model: str) -> dict[str, bool]:
        return {**_DEFAULT_CAPS, **self._caps.get(model, {})}

    def generate(self, model: str, system: str, user: str, max_output_tokens: int,
                 thinking: str = "default") -> RawReply:
        """thinking="minimal" asks the model not to think (if it supports thinkingLevel); "default" lets it think."""
        caps = self.capabilities(model)
        rate_retries = server_retries = 0
        while True:
            try:
                resp = self._client.post(
                    GEMINI_URL.format(model=model),
                    json=self._body(system, user, max_output_tokens, caps, thinking),
                    headers={"x-goog-api-key": self._key},
                )
            except httpx.TimeoutException as exc:
                raise FallbackableError(f"Gemini timed out ({model})") from exc
            except httpx.TransportError as exc:
                raise FallbackableError(f"Gemini connection error ({model}): {type(exc).__name__}") from exc
            if resp.status_code == 200:
                if caps != self.capabilities(model):
                    self._caps[model] = caps
                    self._save_caps()
                return self._parse(resp.json(), model, caps)
            message = _error_message(resp)
            low = message.lower()
            if resp.status_code == 400 and caps["system_instruction"] and ("instruction" in low or "system" in low):
                caps["system_instruction"] = False  # e.g. "Developer instruction is not enabled": inline it
                continue
            if resp.status_code == 400 and caps["json_mode"] and ("json" in low or "mime" in low):
                caps["json_mode"] = False  # validate with pydantic instead
                continue
            if resp.status_code == 400 and caps["thinking_level"] and thinking == "minimal" and "thinking" in low:
                caps["thinking_level"] = False  # model always thinks: keep the larger output budget
                continue
            if resp.status_code == 429:
                if rate_retries < 2:
                    rate_retries += 1
                    self._sleep(min(_retry_delay(resp) or 2.0 ** rate_retries, 10.0))
                    continue
                quota = "; ".join(_quota_details(resp))
                raise FallbackableError(f"Gemini rate limit (429) for {model}" + (f" [{quota}]" if quota else ""))
            if resp.status_code >= 500 and server_retries < 2:
                server_retries += 1
                self._sleep(2.0 ** server_retries)
                continue
            raise ModelError(f"Gemini HTTP {resp.status_code} ({model}): {message}")

    @staticmethod
    def _body(system: str, user: str, max_output_tokens: int, caps: dict[str, bool], thinking: str) -> dict[str, Any]:
        text = user if caps["system_instruction"] else f"{system}\n\n{user}"
        no_thinking = thinking == "minimal" and caps["thinking_level"]
        config: dict[str, Any] = {
            "temperature": 0,
            "maxOutputTokens": max_output_tokens + (0 if no_thinking else THINKING_ALLOWANCE_TOKENS),
        }
        if no_thinking:
            config["thinkingConfig"] = {"thinkingLevel": "minimal"}
        if caps["json_mode"]:
            config["responseMimeType"] = "application/json"
        body: dict[str, Any] = {"contents": [{"role": "user", "parts": [{"text": text}]}], "generationConfig": config}
        if caps["system_instruction"]:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        return body

    @staticmethod
    def _parse(data: dict[str, Any], model: str, caps: dict[str, bool]) -> RawReply:
        candidates = data.get("candidates") or []
        if not candidates:
            raise ModelError(f"Gemini returned no answer ({model}): {data.get('promptFeedback', {})}")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        usage = data.get("usageMetadata") or {}
        finish = candidates[0].get("finishReason", "")
        if not text.strip():
            raise ModelError(f"Gemini returned no visible text ({model}, finish={finish}, "
                             f"thought tokens={usage.get('thoughtsTokenCount')})")
        note = f"{finish}; thought tokens={usage.get('thoughtsTokenCount', 0)}"
        return RawReply(text, model, "gemini", usage.get("promptTokenCount"), usage.get("candidatesTokenCount"), note)

    def _load_caps(self) -> dict[str, dict[str, bool]]:
        try:
            return json.loads(self._caps_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_caps(self) -> None:
        self._caps_file.parent.mkdir(parents=True, exist_ok=True)
        self._caps_file.write_text(json.dumps(self._caps, indent=2), encoding="utf-8")


class OllamaTier:
    name = "ollama"

    def __init__(self, host: str, model: str, num_ctx: int, timeout_s: float = 180.0,
                 transport: httpx.BaseTransport | None = None):
        self.host, self.model, self.num_ctx = host.rstrip("/"), model, num_ctx
        self._client = httpx.Client(timeout=timeout_s, transport=transport)
        self._send_think = True  # ask for no thinking: thought tokens would eat the small local context

    def fits(self, system: str, user: str, max_output_tokens: int) -> bool:
        return self._needed(system, user) + max_output_tokens <= self.num_ctx

    def _needed(self, system: str, user: str) -> int:
        return estimate_tokens(system) + estimate_tokens(user) + PROMPT_OVERHEAD_TOKENS

    def generate(self, system: str, user: str, max_output_tokens: int, json_schema: dict[str, Any] | None) -> RawReply:
        needed = self._needed(system, user)
        if needed + max_output_tokens > self.num_ctx:
            raise ContextBudgetError(
                f"prompt needs about {needed} tokens plus {max_output_tokens} for the reply, "
                f"but OLLAMA_NUM_CTX is {self.num_ctx}"
            )
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
            "format": json_schema or "json",
            "options": {"num_ctx": self.num_ctx, "temperature": 0, "num_predict": max_output_tokens},
            "keep_alive": "10m",
        }
        while True:
            if self._send_think:
                body["think"] = False
            else:
                body.pop("think", None)
            try:
                resp = self._client.post(f"{self.host}/api/chat", json=body)
            except httpx.TimeoutException as exc:
                raise FallbackableError(f"Ollama timed out at {self.host}") from exc
            except httpx.TransportError as exc:
                raise FallbackableError(f"Ollama unreachable at {self.host}: {type(exc).__name__}") from exc
            if resp.status_code == 400 and self._send_think and "think" in resp.text.lower():
                self._send_think = False  # this model/runtime does not accept the think flag
                continue
            break
        if resp.status_code != 200:
            raise ModelError(f"Ollama HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        prompt_eval, eval_count = data.get("prompt_eval_count") or 0, data.get("eval_count") or 0
        if prompt_eval + eval_count >= self.num_ctx:
            raise ModelError(
                f"Ollama context window full (prompt {prompt_eval} + reply {eval_count} >= num_ctx {self.num_ctx}); "
                "the prompt may have been truncated, so the reply was discarded"
            )
        duration = data.get("eval_duration") or 0
        rate = f"{eval_count / (duration / 1e9):.1f} tok/s" if duration else None
        return RawReply((data.get("message") or {}).get("content", ""), self.model, "ollama", prompt_eval, eval_count, rate)


class GemmaClient:
    """Routes each role to the primary tier, falling back to the backup on 429, timeout or connection error."""

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep, cache_dir: Path | None = None):
        self.settings = settings
        self._cache_dir = (cache_dir or settings.cache_dir) / "llm"
        self._slots = threading.BoundedSemaphore(settings.max_concurrency)
        self.tiers: dict[str, GeminiTier | OllamaTier] = {}
        if settings.gemini_ready:
            self.tiers["gemini"] = GeminiTier(settings.gemini_api_key, settings.cache_dir / "gemini_caps.json",
                                              transport=transport, sleep=sleep)
        if settings.ollama_ready:
            self.tiers["ollama"] = OllamaTier(settings.ollama_host, settings.ollama_model, settings.ollama_num_ctx,
                                              transport=transport)

    def order(self) -> list[str]:
        sequence = [self.settings.gemma_provider]
        if self.settings.gemma_fallback not in ("none", self.settings.gemma_provider):
            sequence.append(self.settings.gemma_fallback)
        return [name for name in sequence if name in self.tiers]

    def generate_json(self, role: str, system: str, user_levels: str | list[str], schema: type[T],
                      max_output_tokens: int = 800, allow_cloud: bool = True) -> tuple[T, LLMCall]:
        """Ask Gemma for a JSON object of `schema`. `user_levels` runs from richest to most compact prompt.

        allow_cloud=False (profiles not marked cloud_allowed) keeps the call on the local Ollama tier.
        """
        levels = [user_levels] if isinstance(user_levels, str) else list(user_levels)
        tiers = [t for t in self.order() if allow_cloud or t != "gemini"]
        if not tiers:
            raise ModelError("no Gemma tier is configured for this dataset: set GEMINI_API_KEY "
                             "(cloud-allowed datasets only), or OLLAMA_HOST for the local tier")
        last: ModelError | None = None
        for index, name in enumerate(tiers):
            try:
                return self._run(name, role, system, levels, schema, max_output_tokens, fallback_used=index > 0)
            except FallbackableError as exc:
                last = exc
        assert last is not None
        raise last

    def _run(self, tier_name: str, role: str, system: str, levels: list[str], schema: type[T],
             max_output_tokens: int, fallback_used: bool) -> tuple[T, LLMCall]:
        tier = self.tiers[tier_name]
        if isinstance(tier, OllamaTier):
            model = tier.model
            fitting = [u for u in levels if tier.fits(system, u, max_output_tokens)]
            if not fitting:
                raise ContextBudgetError(
                    f"even the smallest prompt does not fit OLLAMA_NUM_CTX={tier.num_ctx}; raise it or use the Gemini tier"
                )
            user = fitting[0]
        else:
            model = self.settings.gemini_model_for(role)
            user = levels[0]
        estimate = estimate_tokens(system) + estimate_tokens(user)
        variant = model if tier_name == "ollama" else f"{model}|thinking={self.settings.gemini_thinking_for(role)}"
        key = _cache_key(tier_name, variant, role, system, user, schema)
        cached = self._cache_get(key)
        if cached is not None:
            try:
                return schema.model_validate(cached), LLMCall(role=role, tier=tier_name, model=model, latency_ms=0,
                                                              cached=True, fallback_used=fallback_used,
                                                              prompt_tokens_est=estimate)
            except ValidationError:
                pass  # contract changed since the entry was cached
        started = time.perf_counter()
        prompt = user
        for attempt in (1, 2):
            with self._slots:
                if isinstance(tier, OllamaTier):
                    raw = tier.generate(system, prompt, max_output_tokens, schema.model_json_schema())
                else:
                    raw = tier.generate(model, system, prompt, max_output_tokens,
                                        thinking=self.settings.gemini_thinking_for(role))
            try:
                result = schema.model_validate(extract_json(raw.text))
                break
            except (ValueError, ValidationError) as exc:
                if attempt == 2:
                    raise ModelError(f"{tier_name} reply does not match the {schema.__name__} contract: {_short(exc)}") from exc
                prompt = (f"{user}\n\nYour previous reply was invalid ({_short(exc)}). "
                          "Reply again with only the JSON object in the required shape.")
        self._cache_put(key, result.model_dump(mode="json"))
        return result, LLMCall(
            role=role, tier=tier_name, model=model, latency_ms=int((time.perf_counter() - started) * 1000),
            fallback_used=fallback_used, prompt_tokens_est=estimate, prompt_tokens=raw.prompt_tokens,
            output_tokens=raw.output_tokens, note=raw.note,
        )

    def _cache_get(self, key: str) -> dict[str, Any] | None:
        try:
            return json.loads((self._cache_dir / f"{key}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _cache_put(self, key: str, value: dict[str, Any]) -> None:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        (self._cache_dir / f"{key}.json").write_text(json.dumps(value), encoding="utf-8")


def _cache_key(tier: str, model: str, role: str, system: str, user: str, schema: type[BaseModel]) -> str:
    payload = json.dumps([tier, model, role, system, user, schema.__name__], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _short(exc: Exception) -> str:
    return " ".join(str(exc).split())[:200]


def _error_message(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("error", {}).get("message", ""))[:300] or resp.reason_phrase
    except ValueError:
        return resp.text[:300]


def _details(resp: httpx.Response) -> list[dict[str, Any]]:
    try:
        return list(resp.json().get("error", {}).get("details", []) or [])
    except ValueError:
        return []


def _retry_delay(resp: httpx.Response) -> float | None:
    for detail in _details(resp):
        delay = str(detail.get("retryDelay", ""))
        if delay.endswith("s"):
            try:
                return float(delay[:-1])
            except ValueError:
                return None
    return None


def _quota_details(resp: httpx.Response) -> list[str]:
    found = []
    for detail in _details(resp):
        for violation in detail.get("violations", []) or []:
            metric = violation.get("quotaMetric") or violation.get("quotaId")
            if metric:
                found.append(f"{metric}={violation.get('quotaValue', '?')}")
    return found
