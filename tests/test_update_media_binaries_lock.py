"""Offline tests for scripts/update-media-binaries-lock.py.

The network paths (release API, SHA2-256SUMS) are stubbed; what is asserted here
is the contract that matters for a release: the lock parses, drift is detected
and named, and a re-pin cannot silently keep a stale hash.

The lock covers the media binaries the Tauri lane actually bundles, which is
yt-dlp only (ffmpeg/ffprobe are resolved at runtime and pinned in
``src/xpst/media/binaries.py``).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "update-media-binaries-lock.py"
LOCK = REPO_ROOT / "scripts" / "media-binaries.lock"

# The pinned macos-arm64 yt-dlp hash, used as a known-good digest in the re-pin
# tests. It is asserted against the lock at read time by the other test module.
YTDLP_MACOS_ARM64 = "1fa6733c37ea6fb51c99ad8fe785e7b7e5f3246c9b980230329d4fb72ed8d4d6"


def _load_module():
    spec = importlib.util.spec_from_file_location("update_media_binaries_lock", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod():
    return _load_module()


def test_real_lock_parses_into_pinned_rows(mod) -> None:
    lines, rows = mod.parse_lock(LOCK.read_text(encoding="utf-8"))
    assert len(lines) > len(rows)
    # Five platforms, one bundled artifact each: yt-dlp.
    assert len(rows) == 5
    assert {row.artifact for row in rows} == {"yt-dlp"}
    for row in rows:
        assert row.url.startswith("https://github.com/")
        assert len(row.sha256) == 64
        assert row.repo.count("/") == 1
        assert row.asset_name
        assert row.tag


def test_row_parsing_rejects_a_malformed_line(mod) -> None:
    with pytest.raises(ValueError):
        mod.parse_lock("macos-arm64\tyt-dlp\traw\n")


def test_row_exposes_repo_tag_and_asset(mod) -> None:
    _, rows = mod.parse_lock(LOCK.read_text(encoding="utf-8"))
    row = rows[0]
    assert row.repo == "yt-dlp/yt-dlp"
    assert row.tag == "2026.08.19"
    assert row.asset_name == "yt-dlp"
    assert row.platform == "macos-arm64"


def test_check_reports_drift_with_the_upstream_hash(mod, monkeypatch) -> None:
    _, rows = mod.parse_lock(LOCK.read_text(encoding="utf-8"))
    digests = {row.asset_name: "sha256:" + row.sha256 for row in rows}
    digests["yt-dlp"] = "sha256:" + "a" * 64
    monkeypatch.setattr(mod, "upstream_digests", lambda repo, tag, token=None: digests)
    problems, drifted = mod.check_rows(rows)
    # Every platform whose asset name is `yt-dlp` shares that asset name.
    assert len(drifted) == 3
    assert {row.artifact for row in drifted} == {"yt-dlp"}
    assert any("recorded sha256" in p for p in problems)


def test_check_is_clean_when_upstream_matches(mod, monkeypatch) -> None:
    _, rows = mod.parse_lock(LOCK.read_text(encoding="utf-8"))
    digests = {row.asset_name: "sha256:" + row.sha256 for row in rows}
    monkeypatch.setattr(mod, "upstream_digests", lambda repo, tag, token=None: digests)
    problems, drifted = mod.check_rows(rows)
    assert not problems, problems
    assert not drifted


def test_missing_asset_is_named_as_a_problem(mod, monkeypatch) -> None:
    _, rows = mod.parse_lock(LOCK.read_text(encoding="utf-8"))
    monkeypatch.setattr(mod, "upstream_digests", lambda repo, tag, token=None: {})
    problems, drifted = mod.check_rows(rows)
    assert not drifted
    assert problems and "is not an asset of" in problems[0]


def test_main_write_repins_a_stale_hash(mod, monkeypatch, tmp_path) -> None:
    stale = tmp_path / "media-binaries.lock"
    stale.write_text(LOCK.read_text(encoding="utf-8").replace(YTDLP_MACOS_ARM64, "b" * 64), encoding="utf-8")
    _, rows = mod.parse_lock(stale.read_text(encoding="utf-8"))
    digests = {}
    for row in rows:
        digests[row.asset_name] = "sha256:" + row.sha256
    digests["yt-dlp"] = "sha256:" + YTDLP_MACOS_ARM64
    monkeypatch.setattr(mod, "upstream_digests", lambda repo, tag, token=None: digests)
    upstream_sums = {
        "yt-dlp": YTDLP_MACOS_ARM64,
        "yt-dlp_linux_aarch64": "b16e4dab368a816cd05d477d698a605a6ae87ccee1c8ffd38fa21d7254141fcc",
        "yt-dlp.exe": "66674953fe251b89f4d08c5f0e35e0728679bd67ab3d7d05c0562af101dd3e7a",
    }
    monkeypatch.setattr(mod, "ytdlp_sums", lambda repo, tag, token=None: dict(upstream_sums))

    assert mod.main(["--lock", str(stale), "--check"]) == 1
    assert mod.main(["--lock", str(stale), "--write"]) == 0
    _, repinned = mod.parse_lock(stale.read_text(encoding="utf-8"))
    assert repinned[0].sha256 == YTDLP_MACOS_ARM64
    assert mod.main(["--lock", str(stale), "--check"]) == 0


def test_main_write_refuses_when_ytdlp_sums_disagree(mod, monkeypatch, tmp_path) -> None:
    copy = tmp_path / "media-binaries.lock"
    copy.write_text(LOCK.read_text(encoding="utf-8"), encoding="utf-8")
    _, rows = mod.parse_lock(copy.read_text(encoding="utf-8"))
    monkeypatch.setattr(
        mod, "upstream_digests", lambda repo, tag, token=None: {row.asset_name: "sha256:" + row.sha256 for row in rows}
    )
    # The release's own SHA2-256SUMS disagrees with the lock for every asset.
    monkeypatch.setattr(mod, "ytdlp_sums", lambda repo, tag, token=None: {row.asset_name: "c" * 64 for row in rows})
    assert mod.main(["--lock", str(copy), "--check"]) == 1
