"""Phone media ingest: POST /api/media and xpst.media.ingest (M0).

The contract under test is the one docs/MVP-CUT.md §1 states: a multipart upload
gets a size cap, a container sniff from the bytes (never the filename), a probe,
staging under the config dir, a content-addressed id and idempotent replay.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xpst.dashboard.api import create_api_router
from xpst.media import ingest

API_TOKEN = "media-api-token"
API_HEADERS = {"X-API-Token": API_TOKEN}

# A minimal ISO base-media header: box size + 'ftyp' + a known brand. ffprobe
# will fail on it (no moov), which is exactly the honest-degradation path.
FAKE_MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 256


@pytest.fixture(autouse=True)
def _api_token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XPST_API_TOKEN", API_TOKEN)
    monkeypatch.delenv(ingest.MAX_UPLOAD_BYTES_ENV, raising=False)


def _client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.state.xpst_config_dir = str(tmp_path)
    app.include_router(create_api_router(str(tmp_path)))
    return TestClient(app)


def _upload(client: TestClient, data: bytes, *, name: str = "clip.mp4", **form: str):
    return client.post(
        "/api/media",
        files={"file": (name, data, "video/mp4")},
        data=form,
        headers=API_HEADERS,
    )


# ── container sniff: bytes, not the filename ────────────────────────────────


def test_container_is_identified_from_magic_bytes_not_the_extension() -> None:
    assert ingest.sniff_container(FAKE_MP4)["container"] == "mp4"
    mov = b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00" + b"\x00" * 64
    assert ingest.sniff_container(mov)["kind"] == "video"
    assert ingest.sniff_container(mov)["container"] == "mov"
    webm = b"\x1aE\xdf\xa3\x01\x00\x00\x00" + b"webm" + b"\x00" * 64
    assert ingest.sniff_container(webm)["container"] == "webm"
    assert ingest.sniff_container(b"RIFF\x00\x00\x00\x00AVI LIST" + b"\x00" * 32)["container"] == "avi"
    assert ingest.sniff_container(b"\xff\xd8\xff\xe0" + b"\x00" * 32)["container"] == "jpeg"
    assert ingest.sniff_container(b"not a media file at all") is None


def test_upload_refuses_an_unknown_container_with_a_typed_code(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = _upload(client, b"plain text pretending to be a video" * 4)
    assert response.status_code == 415
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == ingest.ERROR_UNSUPPORTED_CONTAINER
    # Nothing half-staged: the staging dir holds no media directory.
    assert not list((tmp_path / "media" / "uploads").glob("med_*"))


# ── cap, staging, probe, digest ─────────────────────────────────────────────


def test_upload_stages_under_the_config_dir_with_a_digest_and_probe_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path) as client:
        response = _upload(client, FAKE_MP4, label="phone clip")
    assert response.status_code == 201, response.text
    media = response.json()["media"]

    staged = Path(media["path"])
    assert staged.is_file()
    assert tmp_path in staged.parents
    assert staged.read_bytes() == FAKE_MP4
    assert media["bytes"] == len(FAKE_MP4)
    assert media["media_id"] == "med_" + media["sha256"][:16]
    assert media["container"] == "mp4"
    assert media["kind"] == "video"
    assert media["media_id"].startswith("med_")
    # The probe either answered or said why — never a fabricated duration.
    assert isinstance(media["probed"], bool)
    if not media["probed"]:
        assert media["probe_error"]
    assert media["probe"]["container"] == "mp4"


def test_upload_cap_is_enforced_and_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ingest.MAX_UPLOAD_BYTES_ENV, "64")
    oversized = FAKE_MP4 + b"\x00" * 1024
    with _client(tmp_path) as client:
        response = _upload(client, oversized)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == ingest.ERROR_TOO_LARGE
    uploads = tmp_path / "media" / "uploads"
    assert not list(uploads.glob("med_*"))
    assert not list(uploads.glob(".incoming-*"))


def test_empty_upload_is_refused(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = _upload(client, b"")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == ingest.ERROR_EMPTY


def test_identical_bytes_are_idempotent_and_a_reused_key_with_new_bytes_conflicts(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as client:
        first = _upload(client, FAKE_MP4, idempotency_key="upload-1")
        assert first.status_code == 201
        media_id = first.json()["media"]["media_id"]

        replay = _upload(client, FAKE_MP4, idempotency_key="upload-1")
        assert replay.status_code == 200
        assert replay.json()["replayed"] is True
        assert replay.json()["media"]["media_id"] == media_id

        # Same bytes, no key: content-addressed, so the same record comes back.
        same = _upload(client, FAKE_MP4)
        assert same.json()["media"]["media_id"] == media_id

        conflict = _upload(client, FAKE_MP4 + b"\x00" * 8, idempotency_key="upload-1")
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == ingest.ERROR_IDEMPOTENCY_CONFLICT


def test_media_record_can_be_read_back_and_unknown_ids_are_404(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        media_id = _upload(client, FAKE_MP4).json()["media"]["media_id"]
        fetched = client.get(f"/api/media/{media_id}", headers=API_HEADERS)
        assert fetched.status_code == 200
        assert fetched.json()["media"]["media_id"] == media_id

        missing = client.get("/api/media/med_doesnotexist", headers=API_HEADERS)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == ingest.ERROR_UNKNOWN_MEDIA


def test_media_upload_requires_a_write_credential(tmp_path: Path) -> None:
    app = FastAPI()
    app.state.xpst_config_dir = str(tmp_path)
    app.include_router(create_api_router(str(tmp_path)))
    with TestClient(app) as client:
        response = client.post("/api/media", files={"file": ("clip.mp4", FAKE_MP4, "video/mp4")})
    assert response.status_code == 401


def test_safe_filename_never_escapes_the_staging_directory() -> None:
    assert ingest.safe_filename("../../etc/passwd") == "passwd"
    assert ingest.safe_filename("My Phone Clip!.mp4") == "My Phone Clip_.mp4"
    assert ingest.safe_filename("") == "upload"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_a_real_video_is_probed_with_duration_and_dimensions(tmp_path: Path) -> None:
    """The acceptance criterion is a probed format shown before confirm."""
    source = tmp_path / "real.mp4"
    subprocess.run(
        [
            shutil.which("ffmpeg") or "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=390x844:d=1",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
    )
    with _client(tmp_path) as client:
        response = _upload(client, source.read_bytes(), name="camera.mp4")
    media = response.json()["media"]
    assert response.status_code == 201, json.dumps(response.json())[:400]
    assert media["probed"] is True
    assert media["container"] == "mp4"
    assert media["probe"]["width"] == 390
    assert media["probe"]["height"] == 844
    assert media["probe"]["duration_s"] and media["probe"]["duration_s"] > 0
    assert media["probe"]["codec"]
