"""KnowledgeConfig — agnostic LLM + embedding configuration, separate from the
XPSTConfig god node. Loadable from ``XPST_KB_*`` environment variables so a user
can point the KB at any OpenAI-compatible endpoint without code changes."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class KnowledgeConfig:
    llm_base_url: str = "http://127.0.0.1:8000/v1"
    llm_model: str = "qwen3.6-35b-a3b"
    llm_api_key: str | None = None
    embed_backend: str = "fastembed"
    embed_model: str = "nomic-embed-text-v1.5"
    embed_base_url: str | None = None
    workspace: str = "default"
    whisper_model: str = "base"

    @classmethod
    def from_env(cls) -> "KnowledgeConfig":
        defaults = cls()

        def _get(name: str, default: str) -> str:
            return os.environ.get(name, default)

        def _opt(name: str, default: str | None) -> str | None:
            val = os.environ.get(name)
            return val if val is not None else default

        return cls(
            llm_base_url=_get("XPST_KB_LLM_BASE_URL", defaults.llm_base_url),
            llm_model=_get("XPST_KB_LLM_MODEL", defaults.llm_model),
            llm_api_key=_opt("XPST_KB_LLM_API_KEY", defaults.llm_api_key),
            embed_backend=_get("XPST_KB_EMBED_BACKEND", defaults.embed_backend),
            embed_model=_get("XPST_KB_EMBED_MODEL", defaults.embed_model),
            embed_base_url=_opt("XPST_KB_EMBED_BASE_URL", defaults.embed_base_url),
            workspace=_get("XPST_KB_WORKSPACE", defaults.workspace),
            whisper_model=_get("XPST_KB_WHISPER_MODEL", defaults.whisper_model),
        )
