"""The pip/wheel install must be able to serve the real web UI.

A wheel used to ship no UI at all: ``/`` served the string placeholder and
``#/phone`` did not exist, so a pip user could pair a phone and never reach the
phone screen (card t_f236481a). These tests pin the repair from both ends:

* ``packaging/hatch_ui.py`` embeds ``ui/dist`` in the wheel as
  ``xpst/dashboard/webui`` and refuses to build a UI-less *release* wheel;
* ``xpst.dashboard.server._ui_dist_dir()`` finds that packaged bundle;
* the placeholder page says plainly that the bundle is missing;
* ``xpst ui`` warns and reports ``ui_built: false`` in its readiness payload.

The real artifact proof (clean ``uv pip install`` of a built wheel serving
``#/phone`` at a 390 px viewport) lives in the card's live evidence and in
``scripts/clean_install_smoke.py``, which fails a wheel with no bundle.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from xpst import cli as cli_module
from xpst.dashboard import server as server_module

from .test_dashboard import _make_config

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = REPO_ROOT / "packaging" / "hatch_ui.py"


def _load_hook_module():
    """Import packaging/hatch_ui.py by path (packaging/ is not a package)."""
    pytest.importorskip("hatchling", reason="hatchling is a build-time dependency")
    spec = importlib.util.spec_from_file_location("_xpst_hatch_ui", HOOK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_ui_build(root: Path) -> Path:
    dist = root / "ui" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><html><body><div id=\"app\">XPST-BUILT-UI</div></body></html>",
        encoding="utf-8",
    )
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    return dist


# ── build hook ──────────────────────────────────────────────────────────────


def test_hook_finds_ui_dist_and_maps_it_into_the_package(tmp_path):
    hook = _load_hook_module()
    dist = _make_ui_build(tmp_path)
    assert hook.locate_ui_dist(tmp_path, environ={}) == dist
    assert hook.WHEEL_UI_TARGET == "xpst/dashboard/webui"


def test_hook_honours_xpst_ui_dist_override(tmp_path):
    hook = _load_hook_module()
    override = tmp_path / "elsewhere" / "build"
    (override / "assets").mkdir(parents=True)
    (override / "index.html").write_text("<html></html>", encoding="utf-8")
    assert hook.locate_ui_dist(tmp_path, environ={"XPST_UI_DIST": str(override)}) == override
    # An override pointing at a non-build directory does not fall through.
    assert hook.locate_ui_dist(tmp_path, environ={"XPST_UI_DIST": str(tmp_path / "nope")}) is None


def test_hook_allows_engine_only_wheel_without_a_build(tmp_path):
    hook = _load_hook_module()
    assert hook.locate_ui_dist(tmp_path, environ={}) is None


class _FakeApp:
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def display_warning(self, message: str) -> None:  # pragma: no cover - trivial
        self.warnings.append(message)

    def display_error(self, message: str) -> None:  # pragma: no cover - trivial
        self.errors.append(message)


class _StubHook:
    """Minimal stand-in for a BuildHookInterface instance (no hatchling build)."""

    def __init__(self, root: Path, target_name: str = "wheel") -> None:
        self.root = str(root)
        self.target_name = target_name
        self.app = _FakeApp()


def _run_hook(monkeypatch, root: Path, *, require_ui: bool | None = None):
    hook_module = _load_hook_module()
    if require_ui is None:
        monkeypatch.delenv(hook_module.REQUIRE_UI_ENV, raising=False)
    else:
        monkeypatch.setenv(hook_module.REQUIRE_UI_ENV, "1" if require_ui else "0")
    monkeypatch.delenv(hook_module.UI_DIST_ENV, raising=False)
    stub = _StubHook(root)
    build_data: dict = {}
    hook_module.CustomBuildHook.initialize(stub, "1.2.1", build_data)
    return stub, build_data


def test_hook_force_includes_the_build_when_present(tmp_path, monkeypatch):
    dist = _make_ui_build(tmp_path)
    stub, build_data = _run_hook(monkeypatch, tmp_path)
    assert build_data["force_include"] == {str(dist): "xpst/dashboard/webui"}
    assert stub.app.warnings == []


def test_hook_warns_but_builds_engine_only_wheel_by_default(tmp_path, monkeypatch):
    stub, build_data = _run_hook(monkeypatch, tmp_path)
    assert build_data.get("force_include", {}) == {}
    assert len(stub.app.warnings) == 1
    assert "ui/dist" in stub.app.warnings[0]


def test_hook_fails_the_build_when_a_ui_build_is_required(tmp_path, monkeypatch):
    hook_module = _load_hook_module()
    with pytest.raises(RuntimeError, match="ui/dist"):
        _run_hook(monkeypatch, tmp_path, require_ui=True)
    assert "npm run build" in hook_module.MISSING_UI_MESSAGE


def test_hook_leaves_the_sdist_alone(tmp_path, monkeypatch):
    _make_ui_build(tmp_path)
    stub, build_data = _run_hook(monkeypatch, tmp_path)
    stub2 = _StubHook(tmp_path, target_name="sdist")
    hook_module = _load_hook_module()
    sdist_data: dict = {}
    hook_module.CustomBuildHook.initialize(stub2, "1.2.1", sdist_data)
    assert sdist_data.get("force_include", {}) == {}


# ── server lookup ───────────────────────────────────────────────────────────


def _client(cfg_dir):
    return TestClient(server_module._create_app(cfg_dir))


def test_packaged_ui_dir_is_served_without_any_env_override(tmp_path, monkeypatch):
    """A wheel install has no repo checkout: the bundle inside the package is enough."""
    packaged = tmp_path / "site-packages" / "xpst" / "dashboard" / "webui"
    (packaged / "assets").mkdir(parents=True)
    (packaged / "index.html").write_text("<html><body>PACKAGED-UI</body></html>", encoding="utf-8")
    (packaged / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    monkeypatch.setattr(server_module, "_PACKAGED_UI_DIR", packaged)
    monkeypatch.delenv("XPST_UI_DIST", raising=False)

    cfg_dir = _make_config(tmp_path)
    client = _client(cfg_dir)
    index = client.get("/")
    assert "PACKAGED-UI" in index.text
    assert "Web UI bundle not found" not in index.text
    assert "script-src 'self'" in index.headers["Content-Security-Policy"]
    assert client.get("/assets/app.js").status_code == 200
    # The JSON API keeps precedence over the static mount.
    assert client.get("/health").status_code == 200


def test_xpst_ui_dist_still_wins_over_the_packaged_bundle(tmp_path, monkeypatch):
    packaged = tmp_path / "pkgsite" / "xpst" / "dashboard" / "webui"
    packaged.mkdir(parents=True)
    (packaged / "index.html").write_text("<html><body>PACKAGED-UI</body></html>", encoding="utf-8")
    override = tmp_path / "override"
    override.mkdir()
    (override / "index.html").write_text("<html><body>OVERRIDE-UI</body></html>", encoding="utf-8")
    monkeypatch.setattr(server_module, "_PACKAGED_UI_DIR", packaged)
    monkeypatch.setenv("XPST_UI_DIST", str(override))

    assert server_module._ui_dist_dir() == override


def test_placeholder_page_states_the_missing_bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(server_module, "_PACKAGED_UI_DIR", tmp_path / "no-webui")
    monkeypatch.setenv("XPST_UI_DIST", str(tmp_path / "no-ui-build"))
    resp = _client(_make_config(tmp_path)).get("/")
    assert resp.status_code == 200
    assert "Web UI bundle not found" in resp.text
    assert "#/phone" in resp.text
    assert "npm run build" in resp.text
    # Still the string page, so scripts stay locked down.
    assert "script-src 'none'" in resp.headers["Content-Security-Policy"]


# ── CLI honesty ─────────────────────────────────────────────────────────────


def _run_ui_cli(monkeypatch, tmp_path, *, ui_built: bool, extra_args: list[str] | None = None, human: bool = True):
    """Run ``xpst ui`` against a fake server that reports readiness immediately."""
    packaged = tmp_path / ("webui" if ui_built else "no-webui")
    packaged.mkdir()
    if ui_built:
        (packaged / "index.html").write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(server_module, "_PACKAGED_UI_DIR", packaged)
    # This checkout may have a real ui/dist; XPST_UI_DIST pins the lookup so the
    # test measures the intended case instead of the developer's build.
    monkeypatch.setenv("XPST_UI_DIST", str(packaged))

    captured: dict = {}

    def fake_serve_ui(*, port, host, config_dir, open_browser, on_started, ui_token=None):
        captured["open_browser"] = open_browser
        on_started(f"http://{host}:{port}")
        captured["done"] = True

    monkeypatch.setattr(server_module, "serve_ui", fake_serve_ui)
    monkeypatch.setenv("XPST_CONFIG_DIR", str(tmp_path))
    # CliRunner's stdout is not a TTY, which would auto-enable JSON; pin the
    # human path so the warning text is what gets measured.
    monkeypatch.setattr(cli_module, "_stdout_is_tty", lambda: human)
    runner = CliRunner()
    result = runner.invoke(
        cli_module.main,
        ["ui", "--no-browser", "--port", "8931", *(extra_args or [])],
        obj={"config_path": None},
    )
    return result, captured


def test_ui_cli_warns_when_no_bundle_exists(tmp_path, monkeypatch):
    result, captured = _run_ui_cli(monkeypatch, tmp_path, ui_built=False)
    assert result.exit_code == 0, result.output
    assert "No web UI build found" in result.output
    assert "npm run build" in result.output
    assert captured["done"] is True


def test_ui_cli_is_quiet_when_the_bundle_exists(tmp_path, monkeypatch):
    result, _ = _run_ui_cli(monkeypatch, tmp_path, ui_built=True)
    assert result.exit_code == 0, result.output
    assert "No web UI build found" not in result.output


def test_ui_cli_readiness_payload_reports_ui_built(tmp_path, monkeypatch):
    result, _ = _run_ui_cli(monkeypatch, tmp_path, ui_built=False, extra_args=["--json"], human=False)
    assert result.exit_code == 0, result.output
    import json

    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["ui_built"] is False
    assert payload["pid"] > 0
    assert payload["url"].endswith(":8931")
