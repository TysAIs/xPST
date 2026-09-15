"""HTTP contracts for durable drafts and the stale-plan post gate.

The compose screen (Tauri/Svelte) talks to these endpoints, so they carry the
two guarantees the card asks for:

* a draft is created, updated, re-validated, listed, and discarded over HTTP,
  and survives a *new client* reading the same config dir (an app restart);
* a post bound to a draft whose plan no longer matches the machine's facts is
  refused with 409 and the reasons, and only runs after an explicit
  ``confirm_stale`` re-confirmation — with the engine never being touched on the
  refusal path.

All fixtures live in ``tmp_path``; nothing reads or writes the real ``~/.xpst``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from xpst.drafts import DRAFT_FILE_NAME

from .test_dashboard_first_run_api import (
    FakeEngine,
    _engine_factory,
    _media_file,
    _open_app,
    _post_config,
)

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


def _save_draft(client: TestClient, media: str, *, caption: str = "hello", platforms: list[str] | None = None, draft_id: str = "") -> dict:
    payload: dict = {
        "media_paths": [media],
        "caption": caption,
        "platforms": platforms if platforms is not None else ["youtube"],
    }
    if draft_id:
        payload["draft_id"] = draft_id
    response = client.post("/api/drafts", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _plan(client: TestClient, draft_id: str, media: str, *, caption: str = "hello") -> dict:
    return client.post(
        "/api/preflight",
        json={"draft_id": draft_id, "media_paths": [media], "caption": caption, "platforms": ["youtube"]},
    ).json()


def _post(client: TestClient, draft_id: str, media: str, **extra: object) -> Any:
    payload = {
        "draft_id": draft_id,
        "media_paths": [media],
        "caption": "hello",
        "platforms": ["youtube"],
    }
    payload.update(extra)
    return client.post("/api/post", json=payload)


# ── Round trip ───────────────────────────────────────────────────────────


def test_draft_round_trip_over_http_survives_a_restart(tmp_path: Path) -> None:
    config_dir = _post_config(tmp_path)
    media = _media_file(tmp_path)

    with _open_app(tmp_path, config_dir) as client:
        saved = _save_draft(client, media, caption="half-written")
        draft_id = saved["draft"]["id"]
        assert saved["ok"] is True
        assert saved["verdict"]["stale"] is False
        assert Path(config_dir, DRAFT_FILE_NAME).is_file()

    # A new client (a restarted app) reads the same config dir.
    with _open_app(tmp_path, config_dir) as client:
        listed = client.get("/api/drafts").json()
        assert [row["draft"]["id"] for row in listed["drafts"]] == [draft_id]
        assert listed["network_calls"] is False

        detail = client.get(f"/api/drafts/{draft_id}").json()
        assert detail["ok"] is True
        assert detail["draft"]["caption"] == "half-written"
        assert detail["draft"]["media_paths"] == [media]
        assert detail["verdict"]["validated_at"] is not None

        removed = client.delete(f"/api/drafts/{draft_id}").json()
        assert removed["deleted"] is True
        assert client.get(f"/api/drafts/{draft_id}").status_code == 404


def test_draft_save_updates_in_place_and_reports_a_recreated_draft(tmp_path: Path) -> None:
    config_dir = _post_config(tmp_path)
    media = _media_file(tmp_path)

    with _open_app(tmp_path, config_dir) as client:
        first = _save_draft(client, media, caption="one")
        again = _save_draft(client, media, caption="two", draft_id=first["draft"]["id"])
        assert again["recreated"] is False
        assert again["draft"]["caption"] == "two"
        assert len(client.get("/api/drafts").json()["drafts"]) == 1

        recreated = _save_draft(client, media, caption="three", draft_id="draft_gone")
        assert recreated["recreated"] is True
        assert recreated["draft"]["id"] == "draft_gone"


def test_over_long_caption_is_refused_without_touching_the_store(tmp_path: Path) -> None:
    config_dir = _post_config(tmp_path)
    media = _media_file(tmp_path)

    with _open_app(tmp_path, config_dir) as client:
        response = client.post(
            "/api/drafts", json={"media_paths": [media], "caption": "x" * 20_001, "platforms": ["youtube"]}
        )

    assert response.status_code == 413
    assert response.json()["saved"] is False
    assert not Path(config_dir, DRAFT_FILE_NAME).exists()


# ── The plan record and its invalidation ─────────────────────────────────


def test_preflight_records_the_plan_against_the_drafts_own_content(tmp_path: Path) -> None:
    config_dir = _post_config(tmp_path)
    media = _media_file(tmp_path)

    with _open_app(tmp_path, config_dir) as client:
        draft_id = _save_draft(client, media)["draft"]["id"]
        planned = _plan(client, draft_id, media)

    assert planned["draft_recorded"] is True, planned
    assert planned["draft"]["planned"] is True
    assert planned["draft"]["stale"] is False
    assert planned["draft"]["plan_recorded_at"] is not None


def test_a_plan_whose_media_disappeared_is_reported_stale_on_resume(tmp_path: Path) -> None:
    config_dir = _post_config(tmp_path)
    media = _media_file(tmp_path)

    with _open_app(tmp_path, config_dir) as client:
        draft_id = _save_draft(client, media)["draft"]["id"]
        assert _plan(client, draft_id, media)["draft_recorded"] is True
        Path(media).unlink()
        detail = client.get(f"/api/drafts/{draft_id}").json()

    assert detail["verdict"]["stale"] is True
    assert "MEDIA_MISSING" in {reason["code"] for reason in detail["verdict"]["reasons"]}


def test_a_plan_whose_credentials_went_away_is_reported_stale(tmp_path: Path) -> None:
    config_dir = _post_config(tmp_path)
    media = _media_file(tmp_path)

    with _open_app(tmp_path, config_dir) as client:
        draft_id = _save_draft(client, media)["draft"]["id"]
        assert _plan(client, draft_id, media)["draft_recorded"] is True
        (tmp_path / "youtube-token.json").unlink()
        detail = client.get(f"/api/drafts/{draft_id}").json()

    assert detail["verdict"]["stale"] is True
    assert "DESTINATION_NOT_READY" in {reason["code"] for reason in detail["verdict"]["reasons"]}


def test_planning_an_unknown_draft_is_reported_not_recorded(tmp_path: Path) -> None:
    config_dir = _post_config(tmp_path)
    media = _media_file(tmp_path)

    with _open_app(tmp_path, config_dir) as client:
        planned = _plan(client, "draft_missing", media)

    assert planned["draft_recorded"] is False
    assert planned["draft"] is None
    assert planned["draft_error"] == "No draft with that id."


# ── The gate on POST /api/post ───────────────────────────────────────────


def test_posting_a_stale_plan_is_refused_and_the_engine_is_never_touched(tmp_path: Path) -> None:
    config_dir = _post_config(tmp_path)
    media = _media_file(tmp_path)
    engine = FakeEngine(succeeds=["youtube"])

    with _open_app(tmp_path, config_dir, engine_factory=_engine_factory(engine)) as client:
        draft_id = _save_draft(client, media)["draft"]["id"]
        # A dry run is a plan: it is recorded against the current facts.
        assert _post(client, draft_id, media, dry_run=True).status_code == 200

        Path(media).unlink()
        response = _post(client, draft_id, media)

    assert response.status_code == 409
    data = response.json()
    assert data["blocked"] is True
    assert data["stale"] is True
    assert data["stale_reasons"], "a refusal must carry the reasons"
    assert all(reason["message"] in data["blockers"] for reason in data["stale_reasons"])
    assert data["uploaded"] is False
    assert engine.calls == [], "a stale plan must not reach the engine"


def test_reconfirming_a_stale_plan_posts_and_records_the_acceptance(tmp_path: Path) -> None:
    """A replaced media file invalidates the plan; re-confirming posts it."""
    config_dir = _post_config(tmp_path)
    media = _media_file(tmp_path)
    engine = FakeEngine(succeeds=["youtube"])

    with _open_app(tmp_path, config_dir, engine_factory=_engine_factory(engine)) as client:
        draft_id = _save_draft(client, media)["draft"]["id"]
        assert _post(client, draft_id, media, dry_run=True).status_code == 200

        # The file was re-encoded/replaced after the plan was made — it is still
        # valid media, so the post itself is fine; the *plan* is not current.
        Path(media).write_bytes(b"\x00" * 8192)

        refused = _post(client, draft_id, media)
        assert refused.status_code == 409
        assert {reason["code"] for reason in refused.json()["stale_reasons"]} == {"MEDIA_CHANGED"}
        assert engine.calls == []

        confirmed = _post(client, draft_id, media, confirm_stale=True)
        stored = client.get(f"/api/drafts/{draft_id}").json()

    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["ok"] is True
    assert confirmed.json()["reconfirmed"] is True
    assert len(engine.calls) == 1, "a re-confirmed post must run exactly once"
    # The post closed the draft out, with the engine's own video id.
    assert stored["draft"]["status"] == "posted"
    assert stored["draft"]["video_id"] == "vid-1"
    assert stored["draft"]["reconfirmations"] == 1


def test_posting_an_unknown_draft_is_refused(tmp_path: Path) -> None:
    config_dir = _post_config(tmp_path)
    media = _media_file(tmp_path)
    engine = FakeEngine(succeeds=["youtube"])

    with _open_app(tmp_path, config_dir, engine_factory=_engine_factory(engine)) as client:
        response = _post(client, "draft_missing", media)

    assert response.status_code == 409
    assert response.json()["unknown_draft"] is True
    assert engine.calls == []


def test_posting_other_content_under_a_draft_id_is_refused(tmp_path: Path) -> None:
    """A draft id must not become a skeleton key for unrelated content."""
    config_dir = _post_config(tmp_path)
    media = _media_file(tmp_path)
    engine = FakeEngine(succeeds=["youtube"])

    with _open_app(tmp_path, config_dir, engine_factory=_engine_factory(engine)) as client:
        draft_id = _save_draft(client, media, caption="the real caption")["draft"]["id"]
        assert _post(client, draft_id, media, dry_run=True).status_code == 200
        response = client.post(
            "/api/post",
            json={"draft_id": draft_id, "media_paths": [media], "caption": "something else", "platforms": ["youtube"]},
        )

    assert response.status_code == 409
    assert "CONTENT_CHANGED" in {reason["code"] for reason in response.json()["stale_reasons"]}
    assert engine.calls == []


def test_a_post_without_a_draft_still_works(tmp_path: Path) -> None:
    """The pre-existing unbound post path is unchanged."""
    config_dir = _post_config(tmp_path)
    media = _media_file(tmp_path)
    engine = FakeEngine(succeeds=["youtube"])

    with _open_app(tmp_path, config_dir, engine_factory=_engine_factory(engine)) as client:
        response = client.post(
            "/api/post", json={"media_paths": [media], "caption": "hello", "platforms": ["youtube"]}
        )

    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert "draft_id" not in response.json()
    assert not Path(config_dir, DRAFT_FILE_NAME).exists(), "an unbound post must not create a draft"
