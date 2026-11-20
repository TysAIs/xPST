"""Tests for Instagram account-type detection and guidance (B2, t_9204c483).

The rules this file pins:

* The account-type decision covers all four verdicts — business, creator,
  personal, unknown — and NEVER guesses: an unreachable or unrecognised
  probe answer is ``unknown``, not ``personal``, not a green light.
* The user-facing message names the exact in-app screens (the same path
  docs/setup-instagram.md Step 1 documents).
* The verified verdict is stored and rides the status payload so the UI can
  show it; an unknown verdict overwrites nothing.
* A known-personal account refuses at upload with the guidance, not an
  opaque Meta error — and an UNPROBED account still attempts the upload.

No personal data: every fixture uses a synthetic 1784140... id and a fake
token; assertions never contain a username-like value from a live account.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from xpst.config import XPSTConfig

from xpst.platforms.instagram_account import (
    SWITCH_STEPS,
    AccountTypeFinding,
    InstagramAccountType,
    classify_graph_payload,
    classify_session_account_info,
    detect_graph_account_type,
    guidance_message,
    persist_account_type,
    unknown_finding,
)

IG_ID = "17841400000000000"
FAKE_TOKEN = "EAAG-fixture-token"

# Meta's verbatim refusal for a personal account (developer-community thread
# 695381956169641; the documented account_type values are Business /
# Media_Creator for professional accounts).
META_PERSONAL_REFUSAL = {
    "error": {
        "message": (
            "Access is denied because the instagram account type is not "
            "business or creator. The instagram account was switched to a "
            "personal account."
        ),
        "type": "OAuthException",
        "code": 200,
    }
}


class TestGraphPayloadDecision:
    """The four-way decision on a Graph API response, from fixtures only."""

    def test_business_field(self) -> None:
        finding = classify_graph_payload({"id": IG_ID, "account_type": "BUSINESS"})
        assert finding.account_type is InstagramAccountType.BUSINESS
        assert finding.publish_ready
        assert not finding.requires_creator_switch

    def test_media_creator_field_maps_to_creator(self) -> None:
        # Meta serves MEDIA_CREATOR for creator accounts on this field.
        finding = classify_graph_payload({"id": IG_ID, "account_type": "MEDIA_CREATOR"})
        assert finding.account_type is InstagramAccountType.CREATOR
        assert finding.publish_ready

    def test_personal_field(self) -> None:
        finding = classify_graph_payload({"id": IG_ID, "account_type": "PERSONAL"})
        assert finding.account_type is InstagramAccountType.PERSONAL
        assert finding.requires_creator_switch
        assert not finding.publish_ready

    def test_personal_refusal_error_decides_personal(self) -> None:
        # The real shape a personal account answers with: the field is only
        # served to professional accounts, so Meta returns an access-denied
        # refusal whose wording is the verdict.
        finding = classify_graph_payload(META_PERSONAL_REFUSAL)
        assert finding.account_type is InstagramAccountType.PERSONAL
        assert finding.source == "graph_error"
        assert finding.requires_creator_switch

    def test_unrelated_error_is_unknown_not_personal(self) -> None:
        finding = classify_graph_payload(
            {"error": {"message": "Invalid OAuth access token - Cannot parse", "code": 190}}
        )
        assert finding.account_type is InstagramAccountType.UNKNOWN
        assert finding.source.startswith("unknown:")
        assert not finding.publish_ready

    def test_unrecognised_field_value_is_unknown(self) -> None:
        finding = classify_graph_payload({"account_type": "SOMETHING_NEW"})
        assert finding.account_type is InstagramAccountType.UNKNOWN

    def test_empty_payload_is_unknown(self) -> None:
        assert classify_graph_payload({}).account_type is InstagramAccountType.UNKNOWN


class TestSessionAccountInfoDecision:
    def test_professional_flag_decides_business(self) -> None:
        finding = classify_session_account_info(
            {"is_business": True, "business_category_name": "DigitalCreator"}
        )
        assert finding.account_type is InstagramAccountType.BUSINESS
        assert finding.publish_ready

    def test_non_professional_decides_personal(self) -> None:
        finding = classify_session_account_info({"is_business": False})
        assert finding.account_type is InstagramAccountType.PERSONAL

    def test_missing_field_is_unknown(self) -> None:
        # A payload that never answered the question must not decide it.
        assert (
            classify_session_account_info({"username_present": True}).account_type
            is InstagramAccountType.UNKNOWN
        )


class TestDetectGraphAccountType:
    def test_invalid_token_classifies_unknown_against_real_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class Resp:
            status_code = 400

            def json(self) -> dict:
                return {"error": {"message": "Invalid OAuth access token - Cannot parse access token", "code": 190, "type": "OAuthException"}}

        import httpx

        monkeypatch.setattr(httpx, "get", lambda *a, **k: Resp())
        finding = detect_graph_account_type(IG_ID, FAKE_TOKEN)
        assert finding.account_type is InstagramAccountType.UNKNOWN

    def test_transport_failure_is_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import httpx

        def boom(*a: object, **k: object) -> None:
            raise httpx.ConnectError("no route", request=httpx.Request("GET", "https://graph.facebook.com"))

        monkeypatch.setattr(httpx, "get", boom)
        finding = detect_graph_account_type(IG_ID, FAKE_TOKEN)
        assert finding.account_type is InstagramAccountType.UNKNOWN
        assert finding.source.startswith("unknown:probe_failed")

    def test_missing_id_or_token_short_circuits(self) -> None:
        # Short-circuits before any import or network call, so no stub is
        # needed: if it ever reaches the wire this would hit a real endpoint.
        assert detect_graph_account_type("", FAKE_TOKEN).account_type is InstagramAccountType.UNKNOWN
        assert detect_graph_account_type(IG_ID, "").account_type is InstagramAccountType.UNKNOWN


class TestGuidanceMessage:
    def test_professional_types_get_no_warning(self) -> None:
        for value in ("business", "creator"):
            finding = AccountTypeFinding(InstagramAccountType(value), source="graph_field")
            assert guidance_message(finding) is None

    def test_personal_message_names_exact_screens_and_recheck(self) -> None:
        finding = AccountTypeFinding(InstagramAccountType.PERSONAL, source="graph_field")
        message = guidance_message(finding)
        assert message is not None
        # One plain-language statement of the requirement...
        assert "Creator" in message
        # ...the exact in-app screens, in order...
        assert "Settings and privacy" in message
        assert "Account type and tools" in message
        assert "Switch to professional account" in message
        for i, step in enumerate(SWITCH_STEPS, 1):
            assert f"{i}. {step}" in message
        # ...and the re-check action.
        assert "xpst connect instagram" in message
        assert "Refresh accounts" in message

    def test_graph_personal_says_publishing_requires_creator(self) -> None:
        finding = AccountTypeFinding(InstagramAccountType.PERSONAL, source="graph_field")
        message = guidance_message(finding, auth_mode="graph_api")
        assert message is not None
        assert "cannot publish from a personal account" in message

    def test_session_personal_does_not_overclaim_publish_block(self) -> None:
        # Session mode CAN still post to a personal account (private API);
        # the message must say that instead of claiming publishing is broken.
        finding = AccountTypeFinding(InstagramAccountType.PERSONAL, source="session_account_info")
        message = guidance_message(finding, auth_mode="session")
        assert message is not None
        assert "cannot publish" not in message
        assert "session path can still post" in message

    def test_unknown_says_unconfirmed_without_claiming_personal(self) -> None:
        message = guidance_message(unknown_finding("probe_failed:ConnectError"))
        assert message is not None
        assert "could not confirm" in message
        assert "personal" not in message


class TestPersistAccountType:
    """Storage rules: verified verdicts persist, unknown never overwrites."""

    def test_verified_verdict_is_stored(self, tmp_path: Path) -> None:
        from xpst.config import XPSTConfig

        config = XPSTConfig()
        config.config_dir = str(tmp_path)
        finding = classify_graph_payload({"account_type": "MEDIA_CREATOR"})
        persist_account_type(config, finding)
        assert config.instagram.account_type == "creator"
        assert config.instagram.account_type_source == "graph_field"
        # and it round-trips through save/load so every surface can show it
        config.save()
        reloaded = XPSTConfig.load(config_path=str(tmp_path / "config.yaml"))
        assert reloaded.instagram.account_type == "creator"
        assert reloaded.instagram.account_type_source == "graph_field"

    def test_unknown_never_overwrites_a_verified_verdict(self, tmp_path: Path) -> None:
        from xpst.config import XPSTConfig

        config = XPSTConfig()
        config.config_dir = str(tmp_path)
        persist_account_type(config, classify_graph_payload({"account_type": "BUSINESS"}))
        persist_account_type(config, unknown_finding("probe_failed:ConnectError"))
        assert config.instagram.account_type == "business"
        assert config.instagram.account_type_source == "graph_field"

    def test_unchanged_verdict_does_not_rewrite_the_file(self, tmp_path: Path) -> None:
        from xpst.config import XPSTConfig

        config = XPSTConfig()
        config.config_dir = str(tmp_path)
        finding = classify_graph_payload({"account_type": "BUSINESS"})
        persist_account_type(config, finding)
        config_path = tmp_path / "config.yaml"
        config.save()
        before = config_path.stat().st_mtime_ns
        persist_account_type(config, finding)
        assert config_path.stat().st_mtime_ns == before



class TestStatusReprobeRule:
    """The post-switch re-check: a missing or personal verdict is re-probed on
    every status refresh; a professional verdict stops costing a call."""

    @staticmethod
    def _config(tmp_path: Path) -> XPSTConfig:
        from xpst.config import XPSTConfig

        config = XPSTConfig()
        config.config_dir = str(tmp_path)
        config.instagram.enabled = True
        config.instagram.auth_mode = "graph_api"
        config.instagram.graph_access_token = FAKE_TOKEN
        config.instagram.graph_ig_user_id = IG_ID
        return config

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("stored", "expect_reprobe"),
        [("", True), ("personal", True), ("business", False), ("creator", False)],
    )
    async def test_reprobe_only_until_professional(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stored: str, expect_reprobe: bool
    ) -> None:
        import xpst.auth_status as auth_status
        import xpst.platforms.instagram_account as ia

        config = self._config(tmp_path)
        config.instagram.account_type = stored
        calls: list[str] = []

        async def fake_probe(token: str):
            return True, None, {}

        def fake_detect(ig_user_id: str, token: str, timeout: float = 15.0):
            calls.append(ig_user_id)
            return AccountTypeFinding(InstagramAccountType.CREATOR, source="graph_field")

        monkeypatch.setattr(auth_status, "_graph_api_probe", fake_probe)
        monkeypatch.setattr(ia, "detect_graph_account_type", fake_detect)

        result = await auth_status.collect_live_auth_status_async(
            config, uploaders={"instagram": _NoopUploader()}
        )

        entry = result["instagram"]
        # After a re-probe the fresh verdict rides the payload; without one,
        # the stored verdict does.
        assert entry["details"]["account_type"] == ("creator" if expect_reprobe else stored)
        assert (IG_ID in calls) is expect_reprobe
        if expect_reprobe:
            # The fresh verdict is stored so the next refresh goes quiet.
            assert config.instagram.account_type == "creator"


class _NoopUploader:
    """Presence placeholder; the graph branch never calls it."""

    async def check_health(self):  # pragma: no cover - must not be reached
        raise AssertionError("graph mode must use the graph probe")

class TestUploaderRefusal:
    """Graph upload refuses a verified personal account with the guidance."""

    @pytest.mark.asyncio
    async def test_personal_stored_verdict_refuses_before_network(self, tmp_path: Path) -> None:
        from xpst.config import XPSTConfig
        from xpst.platforms.instagram import InstagramUploader

        config = XPSTConfig()
        config.config_dir = str(tmp_path)
        config.instagram.enabled = True
        config.instagram.auth_mode = "graph_api"
        config.instagram.graph_access_token = FAKE_TOKEN
        config.instagram.graph_ig_user_id = IG_ID
        config.instagram.account_type = "personal"
        config.instagram.account_type_source = "graph_field"

        uploader = InstagramUploader(config)
        result = await uploader._upload_graph_api(tmp_path / "clip.mp4", "caption")
        assert result.success is False
        assert "Creator" in (result.error or "")
        assert "Account type and tools" in (result.error or "")

    @pytest.mark.asyncio
    async def test_unprobed_account_still_attempts_upload(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # "" (never probed) must NOT be treated as personal — no fabricated
        # refusal. The upload proceeds to the network, which we stub to fail
        # fast, and the failure is the transport one, not the guidance one.
        from xpst.config import XPSTConfig
        from xpst.platforms.instagram import InstagramUploader

        config = XPSTConfig()
        config.config_dir = str(tmp_path)
        config.instagram.enabled = True
        config.instagram.auth_mode = "graph_api"
        config.instagram.graph_access_token = FAKE_TOKEN
        config.instagram.graph_ig_user_id = IG_ID
        assert config.instagram.account_type == ""

        import httpx

        def boom(*a: object, **k: object) -> None:
            raise httpx.ConnectError("stub", request=httpx.Request("POST", "https://graph.facebook.com"))

        monkeypatch.setattr(httpx.AsyncClient, "post", boom)
        monkeypatch.setattr(httpx.AsyncClient, "get", boom)
        uploader = InstagramUploader(config)
        result = await uploader._upload_graph_api(tmp_path / "clip.mp4", "caption")
        assert result.success is False
        assert "Account type and tools" not in (result.error or "")
