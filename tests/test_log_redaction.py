"""No secrets in logs.

The concrete hole this suite pins shut: ``httpx.HTTPStatusError.__str__`` embeds
the **full request URL**, and every Instagram/Meta Graph call in this repo puts
the access token in the query string. So a failed upload running
``logger.error(f"Instagram Graph API HTTP error: {e}")`` wrote a live token into
the log file at ``~/.xpst/logs``.

Every token here is synthetic (``SYNTHETIC_``-prefixed / obviously fake) and is
never a real credential.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from xpst.utils.logger import get_logger
from xpst.utils.redaction import REDACTED, RedactingFormatter, redact_text

# Synthetic only. Matches no real platform's token format.
SYNTHETIC_TOKEN = "SYNTHETIC_IGQV_token_do_not_use_0123456789abcdef"
SYNTHETIC_SESSION = "SYNTHETIC_sessionid_0123456789abcdef"
# Realistic *shape* (digits:alnum, as Telegram embeds in /bot<id>:<secret>/) with
# obviously fake contents.
SYNTHETIC_TG_TOKEN = "999999999:SYNTHETIC_do_not_use_AAAAAAAAAAAAAAAAAAAAAAAAAAA"
SYNTHETIC_BODY = json.dumps(
    {
        "caption": "public caption text",
        "access_token": SYNTHETIC_TOKEN,
        "sessionid": SYNTHETIC_SESSION,
    }
)


def _graph_failure() -> httpx.HTTPStatusError:
    """A failed Meta Graph call whose exception embeds the token in the URL."""
    url = f"https://graph.facebook.com/v19.0/123456/media?access_token={SYNTHETIC_TOKEN}"
    transport = httpx.MockTransport(lambda request: httpx.Response(400, text='{"error":"bad"}'))
    with httpx.Client(transport=transport) as client:
        response = client.post(url, data={"caption": "x"})
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return exc
    raise AssertionError("raise_for_status did not raise")


class TestRedactText:
    def test_access_token_in_url_query_is_redacted(self):
        url = f"https://graph.facebook.com/v19.0/1/media?access_token={SYNTHETIC_TOKEN}"
        out = redact_text(url)
        assert SYNTHETIC_TOKEN not in out
        assert REDACTED in out

    def test_sessionid_in_url_query_is_redacted(self):
        out = redact_text(f"https://example.test/x?sessionid={SYNTHETIC_SESSION}")
        assert SYNTHETIC_SESSION not in out

    def test_cookie_header_is_redacted(self):
        out = redact_text(f"Cookie: sessionid={SYNTHETIC_SESSION}; other=1")
        assert SYNTHETIC_SESSION not in out

    def test_authorization_bearer_is_redacted(self):
        out = redact_text(f"Authorization: Bearer {SYNTHETIC_TOKEN}")
        assert SYNTHETIC_TOKEN not in out

    def test_json_body_secret_fields_are_redacted(self):
        out = redact_text(SYNTHETIC_BODY)
        assert SYNTHETIC_TOKEN not in out
        assert SYNTHETIC_SESSION not in out

    def test_non_secret_text_is_preserved(self):
        text = "Posted to Instagram: https://www.instagram.com/reel/ABC123/"
        assert redact_text(text) == text

    def test_plain_prose_is_untouched(self):
        assert redact_text("upload finished in 12.4s") == "upload finished in 12.4s"

    def test_empty_and_non_string_are_safe(self):
        assert redact_text("") == ""
        assert redact_text(None) is None  # type: ignore[arg-type]


class TestProductPathDoesNotLeak:
    """The real logger xPST hands out must scrub, not just the helper."""

    def test_failed_upload_log_has_no_token(self, caplog):
        with caplog.at_level(logging.DEBUG, logger="xpst.platforms.instagram"):
            logger = get_logger("xpst.platforms.instagram")
            logger.error(f"Instagram Graph API HTTP error: {_graph_failure()}")

        assert SYNTHETIC_TOKEN not in caplog.text
        # Prove it was actually redacted, not merely dropped.
        assert REDACTED in caplog.text

    def test_failed_upload_log_keeps_the_useful_part(self, caplog):
        with caplog.at_level(logging.DEBUG, logger="xpst.platforms.instagram"):
            get_logger("xpst.platforms.instagram").error(
                f"Instagram Graph API HTTP error: {_graph_failure()}"
            )
        assert "Instagram Graph API HTTP error" in caplog.text
        assert "400 Bad Request" in caplog.text

    def test_lazy_string_arg_is_redacted(self, caplog):
        with caplog.at_level(logging.DEBUG, logger="xpst.test.redaction"):
            get_logger("xpst.test.redaction").error("token=%s", SYNTHETIC_TOKEN)
        assert SYNTHETIC_TOKEN not in caplog.text

    def test_urllib_style_webhook_failure_is_redacted(self, caplog):
        with caplog.at_level(logging.DEBUG, logger="xpst.utils.notifications"):
            get_logger("xpst.utils.notifications").warning(
                "Telegram notification failed: "
                f"<urlopen error https://api.telegram.org/bot{SYNTHETIC_TG_TOKEN}/sendMessage>"
            )
        assert SYNTHETIC_TG_TOKEN not in caplog.text

    def test_webhook_url_query_token_is_redacted(self, caplog):
        with caplog.at_level(logging.DEBUG, logger="xpst.utils.notifications"):
            get_logger("xpst.utils.notifications").warning(
                f"Discord webhook failed: https://discord.com/api/webhooks/1?token={SYNTHETIC_TOKEN}"
            )
        assert SYNTHETIC_TOKEN not in caplog.text

    def test_get_logger_is_idempotent(self):
        logger = get_logger("xpst.test.idempotent")
        logger = get_logger("xpst.test.idempotent")
        from xpst.utils.redaction import RedactionFilter

        assert sum(isinstance(f, RedactionFilter) for f in logger.filters) == 1


class TestRedactingFormatter:
    """Defence in depth: tracebacks are scrubbed by the formatter too."""

    @pytest.mark.parametrize(
        "record_factory",
        [
            lambda: logging.LogRecord(
                "xpst.test", logging.ERROR, __file__, 1,
                f"boom access_token={SYNTHETIC_TOKEN}", (), None,
            ),
        ],
    )
    def test_message_is_redacted(self, record_factory):
        out = RedactingFormatter("%(message)s").format(record_factory())
        assert SYNTHETIC_TOKEN not in out

    def test_exception_traceback_is_redacted(self):
        try:
            raise RuntimeError(f"failed with access_token={SYNTHETIC_TOKEN}")
        except RuntimeError:
            import sys

            record = logging.LogRecord(
                "xpst.test", logging.ERROR, __file__, 1, "upload failed", (), sys.exc_info(),
            )
        out = RedactingFormatter("%(message)s").format(record)
        assert SYNTHETIC_TOKEN not in out
        assert "upload failed" in out
