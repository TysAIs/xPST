"""Stream relay: the config that fans one ingest out to many destinations.

The relay is a local feature (MediaMTX moves the bytes), so everything here is
testable without a network: what must hold is that a key cannot leak, that a
misconfigured destination is refused instead of silently dropped, and that the
generated YAML is the shape MediaMTX documents
(``docs/5-forward/5-rtmp.md``: ``forward: [{dest: rtmp(s)://…#streamKey}]``).
"""

from __future__ import annotations

import json
import os
import stat
import sys
from typing import TYPE_CHECKING

import pytest
import yaml
from click.testing import CliRunner

from xpst.stream.destinations import (
    STREAM_INGEST_PATHS,
    api_destinations,
    ingest_path,
    manual_destinations,
    refusal_reason,
    registry_snapshot,
    unsupported_destinations,
)
from xpst.stream.keys import (
    clear_destination,
    destination_status,
    load_destinations,
    store_destination,
)
from xpst.stream.relay import (
    RelayConfigError,
    RelayDestination,
    build_relay_config,
    relay_summary,
    render_relay_config,
    write_relay_config,
)

if TYPE_CHECKING:
    from pathlib import Path

YT_URL = "rtmps://a.rtmps.youtube.com/live2"
TWITCH_URL = "rtmp://live.twitch.tv/app"
YT_KEY = "abcd-1234-efgh-5678"
TWITCH_KEY = "live_99999_secretsecretsecret"


def _yt() -> RelayDestination:
    return RelayDestination("youtube", YT_URL, YT_KEY)


def _twitch() -> RelayDestination:
    return RelayDestination("twitch", TWITCH_URL, TWITCH_KEY)


# ── registry ────────────────────────────────────────────────────────────────


def test_registry_classes_match_the_vendor_contract() -> None:
    """Key-by-API, manual handoff and refusal are three different promises."""
    assert {row.platform for row in api_destinations()} == {"youtube", "twitch", "kick", "facebook"}
    assert {row.platform for row in manual_destinations()} == {"instagram", "tiktok", "rumble"}
    assert "x" in {row.platform for row in unsupported_destinations()}
    for row in api_destinations():
        assert row.endpoint, f"{row.platform} claims an API path with no endpoint"
    for row in manual_destinations():
        assert row.operator_action, f"{row.platform} is manual with no instruction"


def test_every_api_row_names_where_it_comes_from() -> None:
    assert all(row.source_url for row in STREAM_INGEST_PATHS.values() if row.api_available)


def test_registry_snapshot_is_serialisable_and_complete() -> None:
    snapshot = registry_snapshot()
    assert set(snapshot) == {"api", "manual", "unsupported", "refusals"}
    total = sum(len(snapshot[key]) for key in ("api", "manual", "unsupported"))
    assert total == len(STREAM_INGEST_PATHS)


def test_unknown_platform_is_a_bug_not_a_verdict() -> None:
    with pytest.raises(KeyError):
        ingest_path("myspace")


def test_simulcast_refusals_are_data() -> None:
    assert refusal_reason("tiktok_shop")
    assert refusal_reason("youtube") is None


# ── destination validation ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("platform", "url", "key", "match"),
    [
        ("myspace", YT_URL, YT_KEY, "not a registered stream destination"),
        ("x", YT_URL, YT_KEY, "no live ingest path"),
        ("youtube", "https://a.rtmp.youtube.com/live2", YT_KEY, "rtmp:// or rtmps://"),
        ("youtube", "rtmps://", YT_KEY, "no host"),
        ("youtube", YT_URL, "", "stream key is empty"),
        ("youtube", YT_URL, "has space", "whitespace"),
        ("youtube", YT_URL, "key#withhash", "separate"),
    ],
)
def test_bad_destinations_are_refused(platform: str, url: str, key: str, match: str) -> None:
    with pytest.raises(RelayConfigError, match=match):
        RelayDestination(platform, url, key)


def test_forward_url_uses_the_documented_hash_separator() -> None:
    assert _yt().forward_url == f"{YT_URL}#{YT_KEY}"


def test_repr_and_masked_views_do_not_carry_the_key() -> None:
    dest = _yt()
    assert YT_KEY not in repr(dest)
    masked = dest.masked()
    assert masked["platform"] == "youtube"
    assert masked["ingest_url"] == YT_URL
    assert masked["stream_key"] != YT_KEY
    assert YT_KEY not in str(masked)


# ── relay config ────────────────────────────────────────────────────────────


def test_config_contains_one_forward_entry_per_destination() -> None:
    config = build_relay_config([_yt(), _twitch()])
    forward = config["paths"]["xpst"]["forward"]
    assert [entry["dest"] for entry in forward] == [
        f"{YT_URL}#{YT_KEY}",
        f"{TWITCH_URL}#{TWITCH_KEY}",
    ]
    assert config["paths"]["xpst"]["source"] == "publisher"
    assert config["rtmp"] is True
    assert config["rtmpAddress"] == ":1935"
    # The relay is not a public server: every extra protocol stays off —
    # including MoQ, whose default UDP port (:8892) collides with a second
    # instance, which is exactly how this list was found.
    for surface in ("rtsp", "hls", "webrtc", "srt", "moq", "api"):
        assert config[surface] is False


def test_no_destinations_is_a_refusal_not_an_empty_relay() -> None:
    with pytest.raises(RelayConfigError, match="no destinations configured"):
        build_relay_config([])


def test_duplicate_platform_is_refused() -> None:
    with pytest.raises(RelayConfigError, match="configured twice"):
        build_relay_config([_yt(), RelayDestination("youtube", YT_URL, "other-key-0001")])


@pytest.mark.parametrize("path_name", ["", "has space", "with/slash", "-leading"])
def test_relay_path_name_must_be_url_safe(path_name: str) -> None:
    with pytest.raises(RelayConfigError, match="path name"):
        build_relay_config([_yt()], path_name=path_name)


def test_port_must_be_usable() -> None:
    with pytest.raises(RelayConfigError, match="out of range"):
        build_relay_config([_yt()], rtmp_port=70000)


def test_summary_never_contains_a_key() -> None:
    summary = relay_summary([_yt(), _twitch()])
    blob = str(summary)
    assert YT_KEY not in blob
    assert TWITCH_KEY not in blob
    assert summary["relay_ingest_url"] == "rtmp://127.0.0.1:1935/xpst"
    assert summary["destination_count"] == 2


def test_rendered_yaml_parses_and_carries_the_secret_only_in_dest() -> None:
    rendered = render_relay_config(build_relay_config([_yt()]))
    assert "mode 0600" in rendered
    parsed = yaml.safe_load(rendered.split("\n", 4)[4])
    assert parsed["paths"]["xpst"]["forward"][0]["dest"].endswith(f"#{YT_KEY}")


def test_written_config_is_owner_only(tmp_path: Path) -> None:
    target = write_relay_config(tmp_path / "mediamtx.yml", [_yt()])
    text = target.read_text(encoding="utf-8")
    parsed = yaml.safe_load(text.split("\n", 4)[4])
    assert parsed["paths"]["xpst"]["forward"] == [{"dest": f"{YT_URL}#{YT_KEY}"}]
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(target).st_mode) == 0o600


def test_regenerating_over_a_loose_file_narrows_it_to_owner_only(tmp_path: Path) -> None:
    """O_CREAT does not re-apply the mode to an existing file.

    Measured before the fix: a 0644 config left by a copy or a chmod stayed 0644
    after regeneration while carrying live stream keys.
    """
    target = tmp_path / "mediamtx.yml"
    target.write_text("# pre-existing\n", encoding="utf-8")
    os.chmod(target, 0o644)
    write_relay_config(target, [_yt()])
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    assert YT_KEY in target.read_text(encoding="utf-8")


# ── credential store ────────────────────────────────────────────────────────


def test_store_load_clear_round_trip(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    store_destination(config_dir, "youtube", YT_URL, YT_KEY)

    loaded = load_destinations(config_dir)
    assert [dest.platform for dest in loaded] == ["youtube"]
    assert loaded[0].stream_key == YT_KEY

    status = destination_status(config_dir)
    assert status["ready_to_relay"] is True
    assert status["configured"] == ["youtube"]
    assert YT_KEY not in str(status)

    assert clear_destination(config_dir, "youtube") is True
    assert load_destinations(config_dir) == []
    assert destination_status(config_dir)["ready_to_relay"] is False


def test_registry_order_is_preserved_regardless_of_insertion_order(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    store_destination(config_dir, "twitch", TWITCH_URL, TWITCH_KEY)
    store_destination(config_dir, "youtube", YT_URL, YT_KEY)
    assert [dest.platform for dest in load_destinations(config_dir)] == ["youtube", "twitch"]


def test_store_rejects_before_writing(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    with pytest.raises(RelayConfigError):
        store_destination(config_dir, "youtube", YT_URL, "")
    assert load_destinations(config_dir) == []


def test_stored_credential_file_names_are_windows_safe(tmp_path: Path) -> None:
    """The key becomes a file name, and Windows refuses the reserved set.

    A colon in the namespace (``stream_destination:youtube``) reads as harmless
    on Linux/macOS and fails on Windows at the atomic rename with
    ``WinError 87`` — so the assertion is on the characters of the real file
    name, not on the constant that produced it.
    """
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    store_destination(config_dir, "youtube", YT_URL, YT_KEY)
    store_destination(config_dir, "twitch", TWITCH_URL, TWITCH_KEY)

    names = [path.name for path in (config_dir / "credentials").glob("*.enc")]
    assert len(names) == 2, names
    for name in names:
        assert not (set(name) & set('<>:"/\\|?*')), name
        assert name == name.strip(), name


# ── CLI surface ─────────────────────────────────────────────────────────────


def test_cli_destinations_reports_the_matrix(tmp_path: Path) -> None:
    from xpst.cli import main

    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    cfg = config_dir / "config.yaml"
    cfg.write_text("accounts:\n  youtube:\n    enabled: false\n", encoding="utf-8")
    result = CliRunner().invoke(main, ["--config", str(cfg), "stream", "destinations", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output[result.output.index("{") :])
    assert payload["ok"] is True
    assert payload["configured"] == []
    platforms = {row["platform"]: row["ingest_class"] for row in payload["platforms"]}
    assert platforms["youtube"] == "api"
    assert platforms["instagram"] == "manual"
    assert platforms["x"] == "unsupported"


def test_cli_relay_config_writes_a_file_and_reports_no_secret(tmp_path: Path) -> None:
    from xpst.cli import main

    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    store_destination(config_dir, "youtube", YT_URL, YT_KEY)
    cfg = config_dir / "config.yaml"
    cfg.write_text("accounts:\n  youtube:\n    enabled: false\n", encoding="utf-8")
    out = tmp_path / "mediamtx.yml"

    result = CliRunner().invoke(
        main,
        ["--config", str(cfg), "stream", "relay-config", "--output", str(out), "--json"],
    )
    assert result.exit_code == 0, result.output
    assert out.exists()
    assert YT_KEY not in result.output
