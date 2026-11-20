"""Global test configuration for xPST.

Ensures tests never access the real OS keychain (which would hang waiting
for authentication prompts on macOS) and never make real network calls.
"""

import pytest

import xpst.utils.credentials as _cred_mod


@pytest.fixture(autouse=True)
def _no_real_keyring(monkeypatch):
    """Force CredentialStore to skip keyring in every test.

    By setting HAS_KEYRING=False at module level *before* CredentialStore
    is instantiated, the constructor never calls ``keyring.get_password()``
    (which triggers a macOS Keychain auth prompt and hangs).
    """
    monkeypatch.setattr(_cred_mod, "HAS_KEYRING", False)


@pytest.fixture(autouse=True)
def _stub_ffmpeg_verification(request, monkeypatch):
    """Most tests construct VideoProcessor (directly or via the engine), but the
    dev/CI box may not have ffmpeg on PATH. Stub the verification so tests don't
    fail on a missing system binary. Tests that specifically exercise ffmpeg
    detection live in a 'stress' or 'video' module and opt out.
    """
    basename = request.node.fspath.basename
    if any(k in basename for k in ("stress", "video", "cross_platform", "failure_outcomes")):
        return
    from xpst.utils.video import VideoProcessor

    monkeypatch.setattr(VideoProcessor, "_verify_ffmpeg", lambda self: None)


@pytest.fixture(autouse=True)
def _disable_anti_bot_time_checks(request, monkeypatch):
    """Disable anti-bot time-of-day checks in all tests except anti_bot tests.

    Tests run at any hour; the anti-bot time window (8am-11pm) would
    cause random failures depending on when pytest executes.
    """
    if "test_anti_bot" in request.node.fspath.basename:
        return  # don't patch — the anti_bot tests need real behavior
    from xpst.anti_bot import AntiBotProtection

    monkeypatch.setattr(AntiBotProtection, "should_post_now", lambda self: True)
    monkeypatch.setattr(AntiBotProtection, "can_upload", lambda self, platform: True)


@pytest.fixture(autouse=True)
def _no_live_auth_warm(monkeypatch):
    """Never warm the dashboard auth cache during tests.

    Creating an app would otherwise start a background thread that probes real
    platform APIs, making the suite slow and network-dependent. Tests that
    exercise the warm path remove this variable explicitly.
    """
    monkeypatch.setenv("XPST_DISABLE_AUTH_WARM", "1")


@pytest.fixture(autouse=True)
def _no_real_connectivity_probe(monkeypatch):
    """Never let the connectivity probe touch the real network in tests.

    ``xpst doctor`` / ``health`` / ``run`` call ``check_network()``; on a
    networked CI box that would make their output (and the ``all_clear``
    verdict) depend on the runner's internet access. Tests that need the
    offline path re-patch ``xpst.utils.net.check_network`` themselves.
    """
    from xpst.utils.net import NetworkStatus

    monkeypatch.setattr(
        "xpst.utils.net.check_network",
        lambda *a, **kw: NetworkStatus(online=True, detail="online (test stub)"),
    )


@pytest.fixture(autouse=True)
def _isolate_config_dir_env(monkeypatch, tmp_path):
    """Keep the real ``~/.xpst`` out of every test.

    Two failure modes this closes:

    * an ambient ``XPST_CONFIG_DIR`` (an installer/e2e lane exporting it)
      would silently redirect a test's writes away from its ``tmp_path``;
    * and the inverse, the D3 defect class: code that resolves its own
      default directory (``ScheduleManager()``, the MCP audit logger) used to
      land in the invoking user's REAL ``~/.xpst`` — a test-suite
      ``xpst_schedule_cancel`` silently emptied the live queue of the machine
      running pytest (2026-09-28, 10 user entries).

    Pointing the variable at a per-test temp dir closes both: any writer that
    honours the config-dir contract (the documented sandbox hook) now lands in
    throwaway space. Tests that want the shipped default HOME layout (they
    assert ``~/.xpst`` paths) delenv or setenv explicitly, and tests that
    inject their own temp HOME keep working because the temp dir sits under
    their own tmp_path.
    """
    monkeypatch.delenv("XPST_CONFIG_DIR", raising=False)
    # Sibling of tmp_path, not inside it: fixtures that assert "no leftover
    # files in tmp_path" must not see the isolation dir as leakage.
    isolated = tmp_path.parent / f"{tmp_path.name}.xpstcfg"
    isolated.mkdir(parents=True, exist_ok=True)
    # Seed the default config so first-run code paths do not emit the
    # "Created default config" INFO line through the rich handler onto
    # stdout, which corrupts CliRunner JSON-output assertions. Mirrors the
    # writer in xpst/config.py.
    try:
        import yaml

        from xpst.config import DEFAULT_CONFIG
        from xpst.config_migration import ConfigMigration

        (isolated / "config.yaml").write_text(
            yaml.safe_dump(
                {"version": ConfigMigration.CURRENT_VERSION, **DEFAULT_CONFIG},
                sort_keys=False,
            ),
            encoding="utf-8",
        )
    except Exception:  # pragma: no cover - seeding is best-effort
        pass
    monkeypatch.setenv("XPST_CONFIG_DIR", str(isolated))
