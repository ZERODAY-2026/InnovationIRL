"""Runtime settings from environment variables; .env is loaded with python-dotenv.

Secret values are excluded from repr() and never printed. Use public_summary() for logs and the UI.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
ROLES = ("plan", "explain", "insights")
PROVIDERS = ("gemini", "ollama")
THINKING_MODES = ("default", "minimal")
DEFAULT_THINKING = {"plan": "default", "explain": "minimal", "insights": "default"}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


@dataclass(frozen=True)
class Settings:
    snowflake_account: str
    snowflake_user: str
    snowflake_role: str
    snowflake_warehouse: str
    snowflake_database: str
    snowflake_private_key_path: str
    snowflake_private_key_passphrase: str = field(repr=False)
    profile: str = "tpch"
    gemma_provider: str = "gemini"
    gemma_fallback: str = "ollama"
    gemini_api_key: str = field(default="", repr=False)
    gemini_model: str = "gemma-4-26b-a4b-it"
    gemini_role_models: dict[str, str] = field(default_factory=dict)
    # "default" = the model thinks before answering (slower, often better SQL); "minimal" = no thinking.
    gemini_thinking: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_THINKING))
    ollama_host: str = ""
    ollama_model: str = "gemma4:e4b"
    ollama_num_ctx: int = 4096
    max_concurrency: int = 1
    cache_dir: Path = ROOT / ".cache"

    def gemini_model_for(self, role: str) -> str:
        return self.gemini_role_models.get(role) or self.gemini_model

    def gemini_thinking_for(self, role: str) -> str:
        return self.gemini_thinking.get(role, "default")

    @property
    def snowflake_ready(self) -> bool:
        return bool(self.snowflake_account and self.snowflake_user and self.snowflake_private_key_path)

    @property
    def gemini_ready(self) -> bool:
        return bool(self.gemini_api_key)

    @property
    def ollama_ready(self) -> bool:
        return bool(self.ollama_host)

    def public_summary(self) -> dict[str, object]:
        """Settings that are safe to show: no keys, passphrases or key paths."""
        return {
            "profile": self.profile,
            "gemma_provider": self.gemma_provider,
            "gemma_fallback": self.gemma_fallback,
            "gemini_key_set": self.gemini_ready,
            "gemini_models": {role: self.gemini_model_for(role) for role in ROLES},
            "gemini_thinking": {role: self.gemini_thinking_for(role) for role in ROLES},
            "ollama_host_set": self.ollama_ready,
            "ollama_model": self.ollama_model,
            "ollama_num_ctx": self.ollama_num_ctx,
            "snowflake_configured": self.snowflake_ready,
            "snowflake_role": self.snowflake_role,
            "snowflake_warehouse": self.snowflake_warehouse,
        }


def load_settings(env_file: Path | None = None) -> Settings:
    load_dotenv(env_file or ROOT / ".env", override=False)
    provider = _env("GEMMA_PROVIDER", "gemini").lower()
    fallback = _env("GEMMA_FALLBACK", "ollama").lower() or "none"
    if provider not in PROVIDERS:
        raise ValueError(f"GEMMA_PROVIDER must be one of {PROVIDERS}")
    if fallback not in (*PROVIDERS, "none"):
        raise ValueError("GEMMA_FALLBACK must be gemini, ollama or none")
    role_models = {role: _env(f"GEMINI_MODEL_{role.upper()}") for role in ROLES}
    thinking = {role: (_env(f"GEMINI_THINKING_{role.upper()}") or DEFAULT_THINKING[role]).lower() for role in ROLES}
    for role, mode in thinking.items():
        if mode not in THINKING_MODES:
            raise ValueError(f"GEMINI_THINKING_{role.upper()} must be one of {THINKING_MODES}")
    return Settings(
        snowflake_account=_env("SNOWFLAKE_ACCOUNT"),
        snowflake_user=_env("SNOWFLAKE_USER", "DATAMIND_APP"),
        snowflake_role=_env("SNOWFLAKE_ROLE", "DATAMIND_READER"),
        snowflake_warehouse=_env("SNOWFLAKE_WAREHOUSE", "DATAMIND_WH"),
        snowflake_database=_env("SNOWFLAKE_DATABASE", "DATAMIND"),
        snowflake_private_key_path=_env("SNOWFLAKE_PRIVATE_KEY_PATH"),
        snowflake_private_key_passphrase=_env("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE"),
        profile=_env("DATAMIND_PROFILE", "tpch"),
        gemma_provider=provider,
        gemma_fallback=fallback,
        gemini_api_key=_env("GEMINI_API_KEY"),
        gemini_model=_env("GEMINI_MODEL", "gemma-4-26b-a4b-it"),
        gemini_role_models={k: v for k, v in role_models.items() if v},
        gemini_thinking=thinking,
        ollama_host=_env("OLLAMA_HOST").rstrip("/"),
        ollama_model=_env("OLLAMA_MODEL", "gemma4:e4b"),
        ollama_num_ctx=_env_int("OLLAMA_NUM_CTX", 4096),
        max_concurrency=max(1, min(2, _env_int("GEMMA_MAX_CONCURRENCY", 1))),
    )
