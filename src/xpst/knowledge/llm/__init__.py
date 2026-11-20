"""LLM + embedding adapters (OpenAI-compatible). Agnostic to any provider —
reached through ``base_url`` + ``model``. Heavy deps (httpx, fastembed) are
imported lazily inside functions so importing this package stays cheap."""
