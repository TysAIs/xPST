from xpst.knowledge.config import KnowledgeConfig


def test_defaults():
    c = KnowledgeConfig()
    assert c.embed_backend == "fastembed"
    assert c.embed_model == "nomic-ai/nomic-embed-text-v1.5"
    assert c.embed_base_url is None
    assert c.workspace == "default"
    assert c.whisper_model == "base"
    assert c.llm_api_key is None


def test_from_env_overrides(monkeypatch):
    monkeypatch.setenv("XPST_KB_LLM_BASE_URL", "http://127.0.0.1:8000/v1")
    monkeypatch.setenv("XPST_KB_LLM_MODEL", "qwen3.6-35b-a3b")
    monkeypatch.setenv("XPST_KB_LLM_API_KEY", "secret")
    monkeypatch.setenv("XPST_KB_EMBED_BACKEND", "endpoint")
    monkeypatch.setenv("XPST_KB_EMBED_MODEL", "custom-embed")
    monkeypatch.setenv("XPST_KB_EMBED_BASE_URL", "http://127.0.0.1:9000/v1")
    monkeypatch.setenv("XPST_KB_WORKSPACE", "alice")
    monkeypatch.setenv("XPST_KB_WHISPER_MODEL", "small")
    c = KnowledgeConfig.from_env()
    assert c.llm_base_url == "http://127.0.0.1:8000/v1"
    assert c.llm_model == "qwen3.6-35b-a3b"
    assert c.llm_api_key == "secret"
    assert c.embed_backend == "endpoint"
    assert c.embed_model == "custom-embed"
    assert c.embed_base_url == "http://127.0.0.1:9000/v1"
    assert c.workspace == "alice"
    assert c.whisper_model == "small"


def test_from_env_uses_defaults_when_unset(monkeypatch):
    for var in (
        "XPST_KB_LLM_BASE_URL", "XPST_KB_LLM_MODEL", "XPST_KB_LLM_API_KEY",
        "XPST_KB_EMBED_BACKEND", "XPST_KB_EMBED_MODEL", "XPST_KB_EMBED_BASE_URL",
        "XPST_KB_WORKSPACE", "XPST_KB_WHISPER_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)
    c = KnowledgeConfig.from_env()
    assert c.embed_backend == "fastembed"
    assert c.workspace == "default"
    assert c.llm_api_key is None


def test_config_is_frozen():
    import dataclasses

    c = KnowledgeConfig()
    try:
        c.workspace = "x"  # type: ignore[misc]
        raise AssertionError("KnowledgeConfig should be frozen")
    except dataclasses.FrozenInstanceError:
        pass
