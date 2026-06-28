"""KnowledgeConfig — agnostic LLM + embedding configuration, separate from the
XPSTConfig god node. Loadable from ``XPST_KB_*`` environment variables so a user
can point the KB at any OpenAI-compatible endpoint without code changes."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class KnowledgeConfig:
    # D1.2/D1.3: empty by default — user must configure to use LLM
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str | None = None
    # D1.1: LLM disabled by default — KB works with zero config using
    # the deterministic (sentence-segmentation + keyword) extractor.
    # Set llm_enabled=True and configure llm_base_url/llm_model to enable.
    llm_enabled: bool = False
    # Phase 4.2: extraction mode -- "nuggets" (classic teachable points),
    # "clips" (repurposable clip-worthy moments), or "topics" (topic summaries).
    extract_mode: str = "nuggets"
    embed_backend: str = "fastembed"
    embed_model: str = "nomic-ai/nomic-embed-text-v1.5"
    embed_base_url: str | None = None
    workspace: str = "default"
    whisper_model: str = "base"

    @classmethod
    def from_env(cls) -> KnowledgeConfig:
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
            llm_enabled=_get("XPST_KB_LLM_ENABLED", "0").lower()
            not in {"0", "false", "no", "off"},
            extract_mode=_get("XPST_KB_EXTRACT_MODE", defaults.extract_mode),
            embed_backend=_get("XPST_KB_EMBED_BACKEND", defaults.embed_backend),
            embed_model=_get("XPST_KB_EMBED_MODEL", defaults.embed_model),
            embed_base_url=_opt("XPST_KB_EMBED_BASE_URL", defaults.embed_base_url),
            workspace=_get("XPST_KB_WORKSPACE", defaults.workspace),
            whisper_model=_get("XPST_KB_WHISPER_MODEL", defaults.whisper_model),
        )
