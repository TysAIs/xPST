"""Durable compose drafts and stale-plan invalidation.

The two failures these protect against:

* **Lost work** — a half-written post (media, caption, destinations) that died
  when the compose screen was navigated away from or the app restarted.
* **A stale plan posted as if it were current** — a preflight verdict computed
  before a token went missing, a destination was disabled, or a media file was
  replaced, still being postable because nothing recorded what the verdict had
  been computed against.

Every test runs in a ``tmp_path`` config dir: no test reads or writes the real
``~/.xpst``, and no test touches the network.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import pytest

from xpst.config import XPSTConfig
from xpst.drafts import (
    CODE_AUTH_CHANGED,
    CODE_CONTENT_CHANGED,
    CODE_DESTINATION_DISABLED,
    CODE_DESTINATION_NOT_READY,
    CODE_MEDIA_CHANGED,
    CODE_MEDIA_MISSING,
    DRAFT_FILE_NAME,
    MAX_CAPTION_CHARS,
    MAX_DRAFTS,
    DraftService,
    DraftStore,
)


def _config(tmp_path: Path, *, youtube: bool = False, token_body: str = "{}") -> XPSTConfig:
    """A default config bound to ``tmp_path`` (never the real ``~/.xpst``).

    Deterministic: calling it twice with the same ``tmp_path`` yields the same
    paths, which is how a restart is simulated without a config file.
    """
    config = XPSTConfig()
    config.config_dir = str(tmp_path)
    config.local.path = str(tmp_path / "videos")
    Path(config.local.path).mkdir(parents=True, exist_ok=True)
    if youtube:
        _ready_youtube(config, tmp_path, body=token_body)
    return config


def _youtube_token(tmp_path: Path, body: str = "{}") -> Path:
    token = tmp_path / "youtube-token.json"
    token.write_text(body, encoding="utf-8")
    return token


def _ready_youtube(config: XPSTConfig, tmp_path: Path, *, body: str = "{}") -> Path:
    """Configure an enabled destination whose local credentials exist."""
    token = _youtube_token(tmp_path, body=body)
    config.youtube.enabled = True
    config.youtube.token_file = str(token)
    return token


def _media(tmp_path: Path, name: str = "clip.mp4", payload: bytes = b"\x00" * 4096) -> Path:
    path = tmp_path / name
    path.write_bytes(payload)
    return path


def _service(tmp_path: Path, config: XPSTConfig | None = None) -> DraftService:
    return DraftService(config or _config(tmp_path), str(tmp_path))


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """A draft/plan verdict must never open a socket."""

    def _blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("a draft operation attempted a network call")

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)


# ── Round-trip persistence ───────────────────────────────────────────────


def test_draft_survives_navigation_and_app_restart(tmp_path: Path) -> None:
    """Save, throw the writer away, read with a fresh service (a new process)."""
    config = _config(tmp_path, youtube=True)
    media = _media(tmp_path)

    saved = _service(tmp_path, config).save(
        media_paths=[str(media)], caption="half-written post", platforms=["youtube"]
    )
    draft_id = saved["draft"]["id"]

    # A restart means a brand-new service, store, and config object reading the
    # same config dir — exactly what the app does when it is reopened.
    restarted = DraftService(_config(tmp_path, youtube=True), str(tmp_path))
    restored = restarted.get(draft_id)

    assert restored is not None, "the draft did not survive a restart"
    assert restored["caption"] == "half-written post"
    assert restored["media_paths"] == [str(media)]
    assert restored["platforms"] == ["youtube"]
    verdict = restarted.verdict_for(restored)
    assert verdict["stale"] is False
    assert verdict["validated_at"] is not None


def test_drafts_are_listed_newest_first_and_posted_drops_out(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _ready_youtube(config, tmp_path)
    media = _media(tmp_path)
    service = _service(tmp_path, config)

    first = service.save(media_paths=[str(media)], caption="first", platforms=["youtube"])["draft"]
    second = service.save(media_paths=[str(media)], caption="second", platforms=["youtube"])["draft"]

    listed = [row["draft"]["id"] for row in service.list()]
    assert listed == [second["id"], first["id"]]

    service.mark_posted(first["id"], video_id="vid-1")
    remaining = [row["draft"]["id"] for row in service.list()]
    assert remaining == [second["id"]]
    # The posted draft is still on disk (auditable), it is just no longer offered
    # as an in-progress draft.
    assert set(row["draft"]["id"] for row in service.list(include_posted=True)) == {second["id"], first["id"]}
    assert service.get(first["id"])["video_id"] == "vid-1"


def test_store_file_is_private_and_holds_no_credential_material(tmp_path: Path) -> None:
    """Drafts are local-only content; a token must never be copied into one."""
    config = _config(tmp_path)
    secret = "SECRET-TOKEN-VALUE-do-not-persist"
    _ready_youtube(config, tmp_path, body=json.dumps({"access_token": secret}))
    media = _media(tmp_path)

    _service(tmp_path, config).save(
        media_paths=[str(media)], caption="hello", platforms=["youtube"]
    )

    store_path = tmp_path / DRAFT_FILE_NAME
    raw = store_path.read_text(encoding="utf-8")
    assert secret not in raw, "credential material leaked into the draft file"
    assert json.loads(raw)["version"] == 1
    if os.name == "posix":
        assert (store_path.stat().st_mode & 0o777) == 0o600


def test_save_with_an_unknown_id_recreates_instead_of_failing(tmp_path: Path) -> None:
    """A wiped store must not throw away what the user just typed."""
    config = _config(tmp_path)
    _ready_youtube(config, tmp_path)
    media = _media(tmp_path)
    service = _service(tmp_path, config)

    result = service.save(
        draft_id="draft_no_longer_on_disk", media_paths=[str(media)], caption="still here", platforms=["youtube"]
    )

    assert result["recreated"] is True
    assert result["draft"]["id"] == "draft_no_longer_on_disk"
    assert result["draft"]["caption"] == "still here"


def test_a_corrupt_store_reads_as_empty_and_is_kept_aside(tmp_path: Path) -> None:
    path = tmp_path / DRAFT_FILE_NAME
    path.write_text("{not json", encoding="utf-8")

    assert DraftStore(str(tmp_path)).load() == []
    assert not path.exists(), "the corrupt file was left in place"
    quarantined = list(tmp_path.glob("drafts.json.corrupt-*"))
    assert quarantined and quarantined[0].read_text(encoding="utf-8") == "{not json"


def test_store_prunes_to_the_cap(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _ready_youtube(config, tmp_path)
    media = _media(tmp_path)
    service = _service(tmp_path, config)

    for index in range(MAX_DRAFTS + 5):
        service.save(media_paths=[str(media)], caption=f"draft {index}", platforms=["youtube"])

    assert len(DraftStore(str(tmp_path)).load()) == MAX_DRAFTS


def test_an_over_long_caption_is_refused_not_written(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _ready_youtube(config, tmp_path)
    service = _service(tmp_path, config)

    with pytest.raises(ValueError, match="draft limit"):
        service.save(caption="x" * (MAX_CAPTION_CHARS + 1), platforms=["youtube"])

    assert not (tmp_path / DRAFT_FILE_NAME).exists()


# ── Stale-plan detection: one test per precondition the card names ───────


def _planned_service(tmp_path: Path) -> tuple[DraftService, dict, Path, XPSTConfig]:
    """A draft with a recorded, currently-valid plan plus its media file."""
    config = _config(tmp_path)
    _ready_youtube(config, tmp_path)
    media = _media(tmp_path)
    service = _service(tmp_path, config)
    record = service.save(media_paths=[str(media)], caption="post it", platforms=["youtube"])["draft"]
    planned = service.record_plan(
        record["id"], plan={"ready": True, "platforms": {}}, ready=True, blockers=[]
    )
    assert planned is not None
    verdict = service.verdict_for(planned)
    assert verdict["planned"] is True and verdict["stale"] is False
    return service, planned, media, config


def test_a_fresh_plan_is_not_stale(tmp_path: Path, no_network: None) -> None:
    service, record, _media_path, _config_obj = _planned_service(tmp_path)

    verdict = service.revalidate(record["id"])["verdict"]

    assert verdict["stale"] is False
    assert verdict["reasons"] == []
    assert verdict["plan_ready"] is True
    assert verdict["checked_at"] >= verdict["validated_at"]
    assert verdict["current_fingerprint"] == verdict["recorded_fingerprint"]


def test_missing_media_marks_the_plan_stale(tmp_path: Path, no_network: None) -> None:
    service, record, media, _config_obj = _planned_service(tmp_path)

    media.unlink()
    verdict = service.revalidate(record["id"])["verdict"]

    assert verdict["stale"] is True
    assert CODE_MEDIA_MISSING in {reason["code"] for reason in verdict["reasons"]}
    assert any("no longer on disk" in reason["message"] for reason in verdict["reasons"])


def test_replaced_media_marks_the_plan_stale(tmp_path: Path, no_network: None) -> None:
    service, record, media, _config_obj = _planned_service(tmp_path)

    media.write_bytes(b"\x01" * 9000)  # different size and mtime
    verdict = service.revalidate(record["id"])["verdict"]

    assert verdict["stale"] is True
    assert CODE_MEDIA_CHANGED in {reason["code"] for reason in verdict["reasons"]}


def test_disabled_destination_marks_the_plan_stale(tmp_path: Path, no_network: None) -> None:
    service, record, _media_path, config = _planned_service(tmp_path)

    config.youtube.enabled = False
    verdict = service.verdict_for(service.get(record["id"]) or {})

    assert verdict["stale"] is True
    assert CODE_DESTINATION_DISABLED in {reason["code"] for reason in verdict["reasons"]}


def test_lost_credentials_mark_the_plan_stale(tmp_path: Path, no_network: None) -> None:
    """The expired-auth case: the local token file is gone."""
    service, record, _media_path, config = _planned_service(tmp_path)

    Path(config.youtube.token_file).unlink()
    verdict = service.verdict_for(service.get(record["id"]) or {})

    assert verdict["stale"] is True
    codes = {reason["code"] for reason in verdict["reasons"]}
    assert CODE_DESTINATION_NOT_READY in codes
    assert any("Sign in again" in reason["message"] for reason in verdict["reasons"])


def test_changed_sign_in_method_marks_the_plan_stale(tmp_path: Path, no_network: None) -> None:
    config = _config(tmp_path)
    cookies = tmp_path / "x-cookies.json"
    cookies.write_text("[]", encoding="utf-8")
    config.x.enabled = True
    config.x.auth_mode = "cookies"
    config.x.cookies_file = str(cookies)
    media = _media(tmp_path)
    service = _service(tmp_path, config)
    record = service.save(media_paths=[str(media)], caption="post it", platforms=["x"])["draft"]
    service.record_plan(record["id"], plan={}, ready=True, blockers=[])

    config.x.auth_mode = "api_v2"
    config.x.access_token = "token"
    verdict = service.verdict_for(service.get(record["id"]) or {})

    assert verdict["stale"] is True
    assert CODE_AUTH_CHANGED in {reason["code"] for reason in verdict["reasons"]}


def test_editing_the_draft_invalidates_its_plan(tmp_path: Path, no_network: None) -> None:
    service, record, _media_path, _config_obj = _planned_service(tmp_path)

    service.save(draft_id=record["id"], media_paths=record["media_paths"], caption="a different caption", platforms=["youtube"])
    verdict = service.revalidate(record["id"])["verdict"]

    assert verdict["stale"] is True
    assert verdict["content_changed"] is True
    assert verdict["reasons"][0]["code"] == CODE_CONTENT_CHANGED

    # Re-running the check against the new caption clears it: the plan now
    # matches what would be posted.
    service.record_plan(record["id"], plan={}, ready=True, blockers=[])
    assert service.revalidate(record["id"])["verdict"]["stale"] is False


# ── The gate: a stale plan cannot be posted without re-confirmation ──────


def test_stale_plan_is_refused_until_reconfirmed(tmp_path: Path, no_network: None) -> None:
    service, record, media, _config_obj = _planned_service(tmp_path)
    content = {
        "media_paths": record["media_paths"],
        "caption": record["caption"],
        "platforms": record["platforms"],
    }
    media.unlink()

    refused = service.post_gate(record["id"], **content)
    assert refused["allowed"] is False
    assert refused["stale"] is True
    assert refused["reasons"], "a refusal must say why"

    confirmed = service.post_gate(record["id"], **content, confirm_stale=True)
    assert confirmed["allowed"] is True
    assert confirmed["verdict"]["reconfirmed"] is True
    assert confirmed["verdict"]["reconfirmations"] == 1

    # The acceptance is recorded: the re-stamped draft is no longer stale.
    again = service.post_gate(record["id"], **content)
    assert again["allowed"] is True
    assert again["stale"] is False


def test_gate_refuses_content_that_does_not_match_the_draft(tmp_path: Path, no_network: None) -> None:
    """Holding a draft id must not let a caller post something else."""
    service, record, _media_path, _config_obj = _planned_service(tmp_path)

    gate = service.post_gate(
        record["id"], media_paths=record["media_paths"], caption="something else", platforms=["youtube"]
    )

    assert gate["allowed"] is False
    assert CODE_CONTENT_CHANGED in {reason["code"] for reason in gate["reasons"]}


def test_gate_reports_an_unknown_draft(tmp_path: Path, no_network: None) -> None:
    service = _service(tmp_path)

    gate = service.post_gate("draft_missing")

    assert gate["allowed"] is False
    assert gate["unknown_draft"] is True


def test_a_draft_without_a_plan_is_postable(tmp_path: Path, no_network: None) -> None:
    """Only a *plan* is invalidated; an unplanned draft is gated by live preflight."""
    config = _config(tmp_path)
    _ready_youtube(config, tmp_path)
    media = _media(tmp_path)
    service = _service(tmp_path, config)
    record = service.save(media_paths=[str(media)], caption="no plan yet", platforms=["youtube"])["draft"]

    gate = service.post_gate(record["id"], media_paths=[str(media)], caption="no plan yet", platforms=["youtube"])

    assert gate["allowed"] is True
    assert gate["stale"] is False
