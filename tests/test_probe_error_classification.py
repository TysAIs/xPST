"""Probe-failure classification: "rejected" and "unverified" are not the same.

``health``/``doctor``/``auth status`` used to answer every failed Instagram
probe with "Instagram session expired or invalid. Re-run: xpst connect
instagram (username/password required for re-login)" — including transport
errors, 429s, challenges and Instagram's anti-bot 302 redirect loop, none of
which prove anything about the stored credential. The fix is the classifier
pinned here.

The classifier matches exception *names*, so these tests build stand-ins rather
than importing ``instagrapi``/``requests`` — which is exactly why it works in an
install where those optional dependencies are missing.
"""

from __future__ import annotations

import json

import pytest

from xpst.utils.probe_errors import (
    PROBE_INVALID_CREDENTIALS,
    PROBE_UNVERIFIED,
    ProbeFailureError,
    classify_probe_failure,
    probe_details_from_error,
)


class _FakeResponse:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


def _exc(name: str, message: str = "", *, status: int | None = None, body: str = "") -> Exception:
    """An exception whose class name matches a provider library's."""
    response = _FakeResponse(status, body) if status is not None else None
    exc = type(name, (Exception,), {})(message)
    if response is not None:
        exc.response = response  # type: ignore[attr-defined]
    return exc


#: Instagram's real 403 body when it invalidates a session (captured from the
#: wire on the machine that filed t_10a26bfd).
LOGGED_OUT_BODY = json.dumps(
    {
        "error_title": "You've been logged out",
        "error_body": "Please log back in.",
        "message": "login_required",
        "status": "fail",
        "logout_reason": 8,
    }
)

#: Instagram's body when it is throttling the client instead.
THROTTLED_BODY = json.dumps({"message": "Please wait a few minutes before you try again.", "status": "fail"})


@pytest.mark.parametrize(
    ("exc", "expected"),
    (
        # Transport failures never prove a credential is dead.
        (_exc("TooManyRedirects", "Exceeded 30 redirects."), PROBE_UNVERIFIED),
        (_exc("ReadTimeout", "HTTPSConnectionPool: Read timed out."), PROBE_UNVERIFIED),
        (_exc("ConnectionError", "Max retries exceeded"), PROBE_UNVERIFIED),
        (_exc("SSLError", "certificate verify failed"), PROBE_UNVERIFIED),
        # Provider-side throttling / challenges: the credential is fine.
        (_exc("LoginRequired", "login_required", status=403, body=THROTTLED_BODY), PROBE_UNVERIFIED),
        (_exc("PleaseWaitFewMinutes", "Please wait a few minutes"), PROBE_UNVERIFIED),
        (_exc("ChallengeRequired", "challenge_required"), PROBE_UNVERIFIED),
        (_exc("SentryBlock", "Sentry block"), PROBE_UNVERIFIED),
        (_exc("HTTPError", "429 Too Many Requests", status=429), PROBE_UNVERIFIED),
        (_exc("HTTPError", "502 Bad Gateway", status=502), PROBE_UNVERIFIED),
        # A provider that says the session was logged out is a rejection.
        (
            _exc("LoginRequired", "login_required", status=403, body=LOGGED_OUT_BODY),
            PROBE_INVALID_CREDENTIALS,
        ),
        (_exc("BadPassword", "The password you entered is incorrect."), PROBE_INVALID_CREDENTIALS),
        (_exc("InvalidCredentials", "invalid credentials"), PROBE_INVALID_CREDENTIALS),
        # Anything unrecognised is unverified: never assert an expiry that was
        # not observed.
        (_exc("WeirdProviderError", "something new happened"), PROBE_UNVERIFIED),
    ),
)
def test_classification(exc: Exception, expected: str) -> None:
    failure = classify_probe_failure("instagram", exc, probe="sessionid")

    assert failure.kind == expected, failure
    assert failure.retryable is (expected == PROBE_UNVERIFIED)
    assert failure.probe == "sessionid"
    # The raw failure is always carried, never replaced by a paraphrase.
    assert failure.raw_error
    assert failure.raw_error in failure.error


def test_rejection_message_names_the_raw_error_and_stays_actionable() -> None:
    failure = classify_probe_failure(
        "instagram",
        _exc("LoginRequired", "login_required", status=403, body=LOGGED_OUT_BODY),
        probe="sessionid",
    )

    assert failure.kind == PROBE_INVALID_CREDENTIALS
    assert "Re-run: xpst connect instagram" in failure.error
    assert "You've been logged out" in failure.error
    # Instagram's private API is the ban-risky path; say so where the user is
    # being told to re-enter a password.
    assert "Graph API" in failure.error


def test_unverified_message_forbids_the_expiry_claim_and_asks_for_a_retry() -> None:
    failure = classify_probe_failure("instagram", _exc("TooManyRedirects", "Exceeded 30 redirects."))

    assert failure.kind == PROBE_UNVERIFIED
    lowered = failure.error.lower()
    assert "session expired" not in lowered
    assert "unverified" in lowered
    assert "retry" in lowered
    assert "Exceeded 30 redirects." in failure.error


def test_nested_rejection_outranks_the_wrapping_transport_error() -> None:
    """instagrapi's real shape: LoginRequired(403) wrapped in TooManyRedirects."""
    outer = _exc("TooManyRedirects", "Exceeded 30 redirects.")
    outer.__cause__ = _exc(
        "LoginRequired", "login_required", status=403, body=LOGGED_OUT_BODY
    )

    failure = classify_probe_failure("instagram", outer, probe="sessionid")

    assert failure.kind == PROBE_INVALID_CREDENTIALS, failure
    assert "You've been logged out" in failure.raw_error


def test_details_are_attached_for_every_surface() -> None:
    failure = classify_probe_failure("instagram", _exc("ReadTimeout", "Read timed out."))
    details = failure.as_details()

    assert details == {
        "probe": "live",
        "probe_class": PROBE_UNVERIFIED,
        "probe_error": failure.raw_error,
        "probe_retryable": True,
    }

    # `probe` defaults to "live"; the sessionid probe overrides it.
    assert classify_probe_failure("instagram", _exc("ReadTimeout", "x")).probe == "live"


def test_probe_failure_error_keeps_callers_failing_closed() -> None:
    """Every existing caller catches ValueError; this must stay a ValueError."""
    failure = classify_probe_failure("instagram", _exc("ReadTimeout", "Read timed out."))
    error = ProbeFailureError(failure, platform="instagram")

    assert isinstance(error, ValueError)
    assert error.probe_class == PROBE_UNVERIFIED
    assert error.probe_retryable is True
    assert error.remediation == "xpst health"
    assert probe_details_from_error(error)["probe_class"] == PROBE_UNVERIFIED
    # An unclassified error carries no probe fields rather than empty ones.
    assert probe_details_from_error(ValueError("plain")) == {}
