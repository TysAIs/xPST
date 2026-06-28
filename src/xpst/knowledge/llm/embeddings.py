"""Embedding adapters behind a stable ``Embedder`` Protocol. The default
``FastEmbedEmbedder`` runs ONNX on CPU in-process (no PyTorch, ~100MB on disk)
and loads the model lazily on first use. ``EndpointEmbedder`` reaches any
OpenAI-compatible ``/embeddings`` endpoint. Both keep their heavy imports inside
methods so importing this module never pulls in fastembed or httpx."""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from xpst.knowledge.config import KnowledgeConfig


@runtime_checkable
class Embedder(Protocol):
    @property
    def dim(self) -> int: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def _httpx_client(timeout: float):
    """Indirection point so tests can monkeypatch the transport."""
    import httpx  # core dep, lazy

    return httpx.Client(timeout=timeout)


class FastEmbedEmbedder:
    """In-process ONNX/CPU embedder via ``fastembed``. Model loads lazily."""

    def __init__(self, model_name: str = "nomic-ai/nomic-embed-text-v1.5") -> None:
        self.model_name = model_name
        self._model = None
        self._dim: int | None = None

    def _ensure_model(self):
        if self._model is None:
            from fastembed import TextEmbedding  # lazy, heavy

            self._model = TextEmbedding(model_name=self.model_name)
        return self._model

    @property
    def dim(self) -> int:
        if self._dim is None:
            # Determine dimensionality by embedding a probe string once.
            self._dim = len(self.embed(["dimension probe"])[0])
        return self._dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        model = self._ensure_model()
        vectors = [list(map(float, v)) for v in model.embed(list(texts))]
        if vectors:
            self._dim = len(vectors[0])
        return vectors


class EndpointEmbedder:
    """OpenAI-compatible ``/embeddings`` adapter."""

    def __init__(self, base_url: str, model: str,
                 api_key: str | None = None, *, timeout: float = 120.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        # Public so the ingestion manifest records the real model id instead
        # of "unknown" — the re-embed migration keys off this value (G34).
        self.model_name = model
        self._api_key = api_key
        self._timeout = timeout
        self._dim: int | None = None

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    @property
    def dim(self) -> int:
        if self._dim is None:
            self._dim = len(self.embed(["dimension probe"])[0])
        return self._dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        payload = {"model": self._model, "input": list(texts)}
        url = f"{self._base_url}/embeddings"
        with _httpx_client(self._timeout) as http:
            resp = http.post(url, json=payload, headers=self._headers())
            resp.raise_for_status()
            data = resp.json()
        vectors = [list(map(float, row["embedding"])) for row in data["data"]]
        if vectors:
            self._dim = len(vectors[0])
        return vectors


def build_embedder(config: KnowledgeConfig) -> Embedder:
    """Factory: pick an embedder from ``config.embed_backend``."""
    backend = config.embed_backend
    if backend == "fastembed":
        return FastEmbedEmbedder(model_name=config.embed_model)
    if backend == "endpoint":
        if not config.embed_base_url:
            raise ValueError(
                "embed_backend='endpoint' requires embed_base_url to be set"
            )
        return EndpointEmbedder(
            base_url=config.embed_base_url,
            model=config.embed_model,
            api_key=config.llm_api_key,
        )
    raise ValueError(f"Unknown embed_backend: {backend!r}")
