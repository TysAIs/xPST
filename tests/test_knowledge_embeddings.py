import os

import pytest

from xpst.knowledge.config import KnowledgeConfig
from xpst.knowledge.llm.embeddings import (
    EndpointEmbedder,
    FastEmbedEmbedder,
    build_embedder,
)


class FakeEmbedder:
    """Deterministic stand-in: dim-2 vector from char counts."""

    dim = 2

    def embed(self, texts):
        return [[float(len(t)), float(t.count("a"))] for t in texts]


def test_fake_embedder_satisfies_protocol():
    e = FakeEmbedder()
    out = e.embed(["aa", "bbb"])
    assert out == [[2.0, 2.0], [3.0, 0.0]]
    assert e.dim == 2


def test_build_embedder_fastembed_default():
    cfg = KnowledgeConfig()  # embed_backend == "fastembed"
    e = build_embedder(cfg)
    assert isinstance(e, FastEmbedEmbedder)
    assert e.model_name == cfg.embed_model


def test_build_embedder_endpoint():
    cfg = KnowledgeConfig(embed_backend="endpoint",
                          embed_base_url="http://127.0.0.1:9000/v1",
                          embed_model="custom-embed")
    e = build_embedder(cfg)
    assert isinstance(e, EndpointEmbedder)


def test_build_embedder_endpoint_requires_base_url():
    cfg = KnowledgeConfig(embed_backend="endpoint", embed_base_url=None)
    with pytest.raises(ValueError):
        build_embedder(cfg)


def test_build_embedder_unknown_backend():
    cfg = KnowledgeConfig(embed_backend="bogus")
    with pytest.raises(ValueError):
        build_embedder(cfg)


def test_fastembed_does_not_load_model_until_used():
    # Constructing the adapter must not import fastembed.
    e = FastEmbedEmbedder(model_name="nomic-embed-text-v1.5")
    assert e._model is None


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeHttpxClient:
    def __init__(self, payload, capture):
        self._payload = payload
        self._capture = capture

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, json=None, headers=None):
        self._capture["url"] = url
        self._capture["json"] = json
        return _FakeResponse(self._payload)


def test_endpoint_embedder_posts_openai_shape(monkeypatch):
    capture = {}
    payload = {"data": [{"embedding": [0.1, 0.2]}, {"embedding": [0.3, 0.4]}]}

    import xpst.knowledge.llm.embeddings as mod

    monkeypatch.setattr(
        mod, "_httpx_client",
        lambda *a, **k: _FakeHttpxClient(payload, capture),
    )
    e = EndpointEmbedder(base_url="http://x/v1", model="m")
    out = e.embed(["one", "two"])
    assert out == [[0.1, 0.2], [0.3, 0.4]]
    assert capture["url"].endswith("/embeddings")
    assert capture["json"]["model"] == "m"
    assert capture["json"]["input"] == ["one", "two"]
    assert e.dim == 2


@pytest.mark.skipif(
    os.environ.get("RUN_KB_SMOKE") != "1",
    reason="set RUN_KB_SMOKE=1 to run the real fastembed smoke test",
)
def test_real_fastembed_embeds(tmp_path):
    pytest.importorskip("fastembed")
    e = FastEmbedEmbedder(model_name="nomic-embed-text-v1.5")
    out = e.embed(["hello world"])
    assert len(out) == 1
    assert len(out[0]) == e.dim > 0
