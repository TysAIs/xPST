"""Frozen plan lifecycle: preview → confirm → publish → readback (M0).

The rules under test come from docs/PHONE-FIRST-UX.md §2 and §3: no publish
without a confirmation, a changed plan invalidates the confirmation
(``confirm_stale``), a replay returns the original result instead of posting
twice, and a readback never claims "verified" for an id nobody read back.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xpst.dashboard.api import create_api_router
from xpst.media import ingest
from xpst.services import plan_service as plans

REPO_ROOT = Path(__file__).resolve().parents[1]
API_TOKEN = "plan-api-token"
API_HEADERS = {"X-API-Token": API_TOKEN}

FAKE_MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 512


@pytest.fixture(autouse=True)
def _api_token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XPST_API_TOKEN", API_TOKEN)


def _config_dir(tmp_path: Path) -> Path:
    """A config dir seeded from the shipped example (youtube/x enabled)."""
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    shutil.copy(REPO_ROOT / "configs" / "example.yaml", cfg / "config.yaml")
    return cfg


def _staged_media(cfg: Path) -> dict[str, Any]:
    return ingest.stage_upload(
        cfg,
        filename="clip.mp4",
        stream=__import__("io").BytesIO(FAKE_MP4),
    )


def _service(cfg: Path, **kwargs: Any) -> plans.PlanService:
    from xpst.config import XPSTConfig

    return plans.PlanService(XPSTConfig.load(str(cfg / "config.yaml")), cfg, **kwargs)


class _FakePostService:
    """Records every execute() and returns a truthful-looking envelope."""

    def __init__(self) -> None:
        self.executions: list[Any] = []

    def dry_run(self, request: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "dry_run": True,
            "requested": list(request.platforms),
            "destinations": [
                {"platform": name, "attempted": False, "success": None, "post_id": None}
                for name in request.platforms
            ],
        }

    def execute(self, request: Any, *, force_now: bool = False) -> dict[str, Any]:
        self.executions.append(request)
        return {
            "ok": True,
            "dry_run": False,
            "video_id": "vid-123",
            "requested": list(request.platforms),
            "destinations": [
                {
                    "platform": name,
                    "attempted": True,
                    "success": True,
                    "post_id": f"{name}-post-1",
                    "post_url": f"https://example.invalid/{name}/1",
                    "state": "posted",
                }
                for name in request.platforms
            ],
        }


# ── lifecycle ───────────────────────────────────────────────────────────────


def test_create_freezes_a_preview_with_per_destination_states(tmp_path: Path) -> None:
    cfg = _config_dir(tmp_path)
    media = _staged_media(cfg)
    record = _service(cfg).create(
        media_ids=[media["media_id"]], caption="hello", platforms=["youtube", "x"]
    )

    assert record["state"] == plans.STATE_PREVIEWED
    assert record["plan_id"].startswith("plan_")
    assert record["digest"]
    assert record["media"][0]["sha256"] == media["sha256"]
    assert {row["platform"] for row in record["preview"]["route"]} == {"youtube", "x"}
    assert record["preview"]["network_calls"] is False
    # The plan on disk is readable through GET and survives a new service instance.
    assert _service(cfg).get(record["plan_id"])["plan_id"] == record["plan_id"]


def test_publish_requires_a_confirmation(tmp_path: Path) -> None:
    cfg = _config_dir(tmp_path)
    record = _service(cfg).create(
        media_ids=[_staged_media(cfg)["media_id"]], caption="hi", platforms=["youtube"]
    )
    with pytest.raises(plans.PlanError) as excinfo:
        _service(cfg).publish(record["plan_id"], dry_run=True)
    assert excinfo.value.code == plans.ERROR_NOT_CONFIRMED


def test_a_dry_run_publish_does_not_mark_the_plan_published(tmp_path: Path) -> None:
    cfg = _config_dir(tmp_path)
    service = _service(cfg)
    record = service.create(
        media_ids=[_staged_media(cfg)["media_id"]], caption="hi", platforms=["youtube"]
    )
    service.confirm(record["plan_id"], confirm_stale=True)
    planned = service.publish(record["plan_id"], dry_run=True)
    assert planned["state"] == plans.STATE_CONFIRMED
    assert planned["dry_run_result"]["ok"] is True
    # And it is still publishable for real afterwards.
    planned_again = service.publish(record["plan_id"], dry_run=True)
    assert planned_again["replayed"] is True


def test_publish_runs_once_and_a_replay_returns_the_original_result(tmp_path: Path) -> None:
    cfg = _config_dir(tmp_path)
    fake = _FakePostService()
    service = _service(cfg, post_service_factory=lambda: fake)
    record = service.create(
        media_ids=[_staged_media(cfg)["media_id"]], caption="hi", platforms=["youtube", "x"]
    )
    service.confirm(record["plan_id"], confirm_stale=True)

    published = service.publish(record["plan_id"])
    assert published["state"] == plans.STATE_PUBLISHED
    assert published["result"]["destinations"][0]["post_id"] == "youtube-post-1"
    assert len(fake.executions) == 1

    replay = _service(cfg, post_service_factory=lambda: fake).publish(record["plan_id"])
    assert replay["replayed"] is True
    assert replay["result"]["destinations"][0]["post_id"] == "youtube-post-1"
    assert len(fake.executions) == 1  # no second upload


def test_readback_reports_ids_and_never_upgrades_to_verified_without_a_read(
    tmp_path: Path,
) -> None:
    cfg = _config_dir(tmp_path)
    fake = _FakePostService()
    service = _service(cfg, post_service_factory=lambda: fake)
    record = service.create(
        media_ids=[_staged_media(cfg)["media_id"]], caption="hi", platforms=["youtube", "x"]
    )
    service.confirm(record["plan_id"], confirm_stale=True)
    service.publish(record["plan_id"])

    rows = service.readback(record["plan_id"])["rows"]
    assert {row["platform"] for row in rows} == {"youtube", "x"}
    for row in rows:
        assert row["platform_id"] == f"{row['platform']}-post-1"
        assert row["url"]
        assert row["verification"] == plans.SUBMITTED_UNCONFIRMED
        assert row["verification_reason"]

    seen: list[tuple[str, str]] = []

    def _verify(platform: str, post_id: str) -> dict[str, Any]:
        seen.append((platform, post_id))
        return {"verification": plans.VERIFIED, "reason": "read back", "url": f"https://real/{platform}"}

    verified = _service(cfg, post_service_factory=lambda: fake, verify_fn=_verify).readback(
        record["plan_id"], verify=True
    )
    assert seen and len(seen) == 2
    assert all(row["verification"] == plans.VERIFIED for row in verified["rows"])
    assert verified["rows"][0]["url"].startswith("https://real/")


def test_a_changed_media_file_makes_the_plan_stale(tmp_path: Path) -> None:
    cfg = _config_dir(tmp_path)
    media = _staged_media(cfg)
    service = _service(cfg)
    record = service.create(media_ids=[media["media_id"]], caption="hi", platforms=["youtube"])

    Path(media["path"]).write_bytes(FAKE_MP4 + b"different")
    with pytest.raises(plans.PlanError) as excinfo:
        service.confirm(record["plan_id"])
    assert excinfo.value.code == plans.ERROR_STALE
    assert "media changed" in excinfo.value.message

    # An explicit re-confirmation accepts the new state.
    reconfirmed = service.confirm(record["plan_id"], confirm_stale=True)
    assert reconfirmed["state"] == plans.STATE_CONFIRMED
    assert reconfirmed["stale_reconfirmed"] is True


# ── HTTP surface ────────────────────────────────────────────────────────────


def _client(cfg: Path) -> TestClient:
    app = FastAPI()
    app.state.xpst_config_dir = str(cfg)
    app.include_router(create_api_router(str(cfg)))
    return TestClient(app)


def test_http_flow_preview_confirm_publish_dry_run_readback(tmp_path: Path) -> None:
    cfg = _config_dir(tmp_path)
    media = _staged_media(cfg)
    with _client(cfg) as client:
        created = client.post(
            "/api/plans",
            json={"media_ids": [media["media_id"]], "caption": "hello", "platforms": ["youtube", "x"]},
            headers=API_HEADERS,
        )
        assert created.status_code == 201, created.text
        plan_id = created.json()["plan_id"]
        digest = created.json()["digest"]

        assert client.get(f"/api/plans/{plan_id}", headers=API_HEADERS).status_code == 200

        stale = client.post(f"/api/plans/{plan_id}/confirm", json={"digest": "nope"}, headers=API_HEADERS)
        assert stale.status_code == 409
        assert stale.json()["code"] == plans.ERROR_STALE

        confirmed = client.post(
            f"/api/plans/{plan_id}/confirm", json={"digest": digest}, headers=API_HEADERS
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["state"] == plans.STATE_CONFIRMED

        planned = client.post(
            f"/api/plans/{plan_id}/publish", json={"dry_run": True}, headers=API_HEADERS
        )
        assert planned.status_code == 200, planned.text
        assert planned.json()["dry_run_result"]["ok"] is True

        readback = client.get(f"/api/plans/{plan_id}/readback", headers=API_HEADERS)
        assert readback.status_code == 200
        assert {row["platform"] for row in readback.json()["rows"]} == {"youtube", "x"}
        assert all(row["verification"] != plans.VERIFIED for row in readback.json()["rows"])


def test_http_unknown_plan_is_404_and_create_requires_a_credential(tmp_path: Path) -> None:
    cfg = _config_dir(tmp_path)
    with _client(cfg) as client:
        assert client.get("/api/plans/plan_nope", headers=API_HEADERS).status_code == 404
        anonymous = client.post("/api/plans", json={"media_paths": ["/tmp/x.mp4"]})
    assert anonymous.status_code == 401


def test_http_plan_without_media_is_refused(tmp_path: Path) -> None:
    cfg = _config_dir(tmp_path)
    with _client(cfg) as client:
        response = client.post("/api/plans", json={"caption": "hi", "platforms": ["youtube"]}, headers=API_HEADERS)
    assert response.status_code == 400
    assert response.json()["code"] == plans.ERROR_NO_MEDIA
    assert "media" in json.dumps(response.json())


class _DeferredPostService:
    """A post service where one destination is cooling down and one posted."""

    def __init__(self) -> None:
        self.executions = 0

    def dry_run(self, request: Any) -> dict[str, Any]:
        return {"ok": True, "dry_run": True, "requested": list(request.platforms), "destinations": []}

    def execute(self, request: Any, *, force_now: bool = False) -> dict[str, Any]:
        self.executions += 1
        return {
            "ok": False,
            "dry_run": False,
            "video_id": "vid-deferred",
            "requested": list(request.platforms),
            "destinations": [
                {
                    "platform": "youtube",
                    "attempted": True,
                    "success": True,
                    "post_id": "yt-1",
                    "post_url": "https://youtu.be/yt-1",
                    "state": "posted",
                },
                {
                    "platform": "x",
                    "attempted": True,
                    "success": False,
                    "error": "RATE_LIMIT_COOLDOWN: x cooling, resume after 2026-11-07T23:45:00",
                    "state": "deferred",
                    "resume_after": "2026-11-07T23:45:00",
                },
            ],
            "failed_count": 0,
            "deferred": ["x"],
            "deferred_count": 1,
        }


def test_a_cooldown_destination_reads_back_as_deferred_with_its_window(tmp_path: Path) -> None:
    """M0 acceptance: a cooldown is reported as deferred (with the window), not failed."""
    cfg = _config_dir(tmp_path)
    fake = _DeferredPostService()
    service = _service(cfg, post_service_factory=lambda: fake)
    record = service.create(
        media_ids=[_staged_media(cfg)["media_id"]], caption="hi", platforms=["youtube", "x"]
    )
    service.confirm(record["plan_id"], confirm_stale=True)
    service.publish(record["plan_id"])

    rows = {row["platform"]: row for row in service.readback(record["plan_id"])["rows"]}
    assert rows["youtube"]["verification"] == plans.SUBMITTED_UNCONFIRMED
    assert rows["youtube"]["platform_id"] == "yt-1"
    assert rows["x"]["verification"] == plans.DEFERRED
    assert rows["x"]["state"] == "deferred"
    assert rows["x"]["resume_after"] == "2026-11-07T23:45:00"
    assert "resume after 2026-11-07T23:45:00" in rows["x"]["verification_reason"]
    assert rows["x"]["verification"] != plans.FAILED


def test_a_stale_confirm_carries_its_reasons_and_the_new_preview(tmp_path: Path) -> None:
    """A 409 must be actionable: the client gets the reasons and the current plan."""
    cfg = _config_dir(tmp_path)
    media = _staged_media(cfg)
    service = _service(cfg)
    record = service.create(media_ids=[media["media_id"]], caption="hi", platforms=["youtube"])
    Path(media["path"]).write_bytes(FAKE_MP4 + b"changed")

    with _client(cfg) as client:
        response = client.post(f"/api/plans/{record['plan_id']}/confirm", json={}, headers=API_HEADERS)
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == plans.ERROR_STALE
    assert body["stale_reasons"]
    assert body["preview"]["route"]
    assert body["digest"] != record["digest"]


class _CooldownUntilRetryPostService:
    """X is cooling for the first attempt, and publishes on the retry."""

    def __init__(self) -> None:
        self.requests: list[list[str]] = []
        self.cooling = True

    def dry_run(self, request: Any) -> dict[str, Any]:
        return {"ok": True, "dry_run": True, "requested": list(request.platforms), "destinations": []}

    def execute(self, request: Any, *, force_now: bool = False) -> dict[str, Any]:
        self.requests.append(list(request.platforms))
        rows = []
        for name in request.platforms:
            if name == "x" and self.cooling:
                rows.append(
                    {
                        "platform": "x",
                        "attempted": True,
                        "success": False,
                        "state": "deferred",
                        "error": "RATE_LIMIT_COOLDOWN: x cooling, resume after 2026-11-07T23:45:00",
                        "resume_after": "2026-11-07T23:45:00",
                    }
                )
            else:
                rows.append(
                    {
                        "platform": name,
                        "attempted": True,
                        "success": True,
                        "post_id": f"{name}-post-{len(self.requests)}",
                        "post_url": f"https://example.invalid/{name}/{len(self.requests)}",
                        "state": "posted",
                    }
                )
        return {
            "ok": False,
            "dry_run": False,
            "video_id": f"vid-{len(self.requests)}",
            "requested": list(request.platforms),
            "destinations": rows,
        }


def test_a_retry_after_the_window_publishes_the_deferred_destination_exactly_once(
    tmp_path: Path,
) -> None:
    """docs/PHONE-FIRST-UX.md §7.4: deferred, then retry publishes exactly once."""
    cfg = _config_dir(tmp_path)
    fake = _CooldownUntilRetryPostService()
    service = _service(cfg, post_service_factory=lambda: fake)
    record = service.create(
        media_ids=[_staged_media(cfg)["media_id"]], caption="hi", platforms=["youtube", "x"]
    )
    service.confirm(record["plan_id"], confirm_stale=True)

    first = service.publish(record["plan_id"])
    # Not terminal: the deferred destination is still waiting.
    assert first["state"] == plans.STATE_CONFIRMED
    assert fake.requests == [["youtube", "x"]]
    rows = {row["platform"]: row for row in first["result"]["destinations"]}
    assert rows["youtube"]["state"] == "posted"
    assert rows["x"]["state"] == "deferred"

    # The window passes; the retry publishes only the deferred destination.
    fake.cooling = False
    second = service.publish(record["plan_id"])
    assert second["state"] == plans.STATE_PUBLISHED
    assert fake.requests == [["youtube", "x"], ["x"]]
    readback = {row["platform"]: row for row in service.readback(record["plan_id"])["rows"]}
    assert readback["youtube"]["platform_id"] == "youtube-post-1"
    assert readback["x"]["platform_id"] == "x-post-2"
    assert readback["x"]["verification"] == plans.SUBMITTED_UNCONFIRMED

    # A third call is a pure replay: nothing is uploaded again.
    third = service.publish(record["plan_id"])
    assert third["replayed"] is True
    assert fake.requests == [["youtube", "x"], ["x"]]
