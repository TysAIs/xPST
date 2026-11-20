#!/usr/bin/env python3
"""Capture OAuth 1.0a signature vectors for the Rust X provider, independently.

The point is independence: this script implements RFC 5849 §3.4 with Python's
``hmac``/``hashlib`` only — it does not import the Rust crate, authlib, requests
or anything xPST ships — so the Rust test that consumes this fixture is checking
against a second implementation rather than against itself.

Run from the repository root::

    python3 scripts/capture_x_oauth1_vectors.py

Re-running must reproduce ``crates/xpst-provider-x/tests/fixtures/oauth1_vectors.json``
byte-for-byte. If it does not, the fixture was edited by hand.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path
from urllib.parse import quote

FIXTURE = Path("crates/xpst-provider-x/tests/fixtures/oauth1_vectors.json")

# Fake credentials: enough to exercise the key derivation, nothing real.
CREDENTIALS = {
    "consumer_key": "ck-test-0001",
    "consumer_secret": "cs-test-0001",
    "access_token": "at-test-0001",
    "access_token_secret": "as-test-0001",
}


def encode(value: str) -> str:
    """RFC 3986 percent-encoding with the unreserved set intact."""
    return quote(value, safe="-._~")


def signature_base(method: str, url: str, params: list[tuple[str, str]]) -> str:
    encoded = sorted((encode(key), encode(value)) for key, value in params)
    param_string = "&".join(f"{key}={value}" for key, value in encoded)
    base_url = url.split("?", 1)[0]
    return "&".join([method.upper(), encode(base_url), encode(param_string)])


def sign(method: str, url: str, params: list[tuple[str, str]]) -> str:
    key = f"{encode(CREDENTIALS['consumer_secret'])}&{encode(CREDENTIALS['access_token_secret'])}"
    digest = hmac.new(key.encode(), signature_base(method, url, params).encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def authorization_header(
    method: str,
    url: str,
    body_params: list[tuple[str, str]],
    nonce: str,
    timestamp: int,
) -> str:
    oauth = [
        ("oauth_consumer_key", CREDENTIALS["consumer_key"]),
        ("oauth_nonce", nonce),
        ("oauth_signature_method", "HMAC-SHA1"),
        ("oauth_timestamp", str(timestamp)),
        ("oauth_token", CREDENTIALS["access_token"]),
        ("oauth_version", "1.0"),
    ]
    oauth.sort()
    params = oauth + [(key, value) for key, value in body_params]
    signature = sign(method, url, params)
    oauth = sorted(oauth + [("oauth_signature", signature)])
    fields = ", ".join(f'"{encode(key)}"="{encode(value)}"' for key, value in oauth)
    return f"OAuth {fields}"


CASES = [
    {
        "name": "create_post_json_body_signs_oauth_only",
        "method": "POST",
        "url": "https://api.twitter.com/2/tweets",
        "body_params": [],
        "nonce": "nonce-create-0001",
        "timestamp": 1780000000,
    },
    {
        "name": "upload_init_form_params_are_signed",
        "method": "POST",
        "url": "https://upload.twitter.com/1.1/media/upload.json",
        "body_params": [
            ["command", "INIT"],
            ["media_type", "video/mp4"],
            ["total_bytes", "10485760"],
        ],
        "nonce": "nonce-init-0001",
        "timestamp": 1780000001,
    },
    {
        "name": "upload_append_form_params_are_signed",
        "method": "POST",
        "url": "https://upload.twitter.com/1.1/media/upload.json",
        "body_params": [
            ["command", "APPEND"],
            ["media_id", "1234567890123456789"],
            ["segment_index", "0"],
        ],
        "nonce": "nonce-append-0001",
        "timestamp": 1780000002,
    },
    {
        "name": "encoding_of_reserved_and_unicode_values",
        "method": "POST",
        "url": "https://api.twitter.com/2/tweets",
        "body_params": [["note", "a b+c/d~é&x=1"]],
        "nonce": "nonce-encode-0001",
        "timestamp": 1780000003,
    },
]


def main() -> None:
    cases = []
    for case in CASES:
        body_params = [(key, value) for key, value in case["body_params"]]
        params = [
            ("oauth_consumer_key", CREDENTIALS["consumer_key"]),
            ("oauth_nonce", case["nonce"]),
            ("oauth_signature_method", "HMAC-SHA1"),
            ("oauth_timestamp", str(case["timestamp"])),
            ("oauth_token", CREDENTIALS["access_token"]),
            ("oauth_version", "1.0"),
        ] + body_params
        cases.append(
            {
                **case,
                "credentials": CREDENTIALS,
                "expected_signature": sign(case["method"], case["url"], params),
                "expected_header": authorization_header(
                    case["method"],
                    case["url"],
                    body_params,
                    case["nonce"],
                    case["timestamp"],
                ),
            }
        )

    payload = {
        "generated_by": "scripts/capture_x_oauth1_vectors.py",
        "reference": "RFC 5849 section 3.4, HMAC-SHA1",
        "cases": cases,
    }
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(f"wrote {FIXTURE} ({len(cases)} cases)")


if __name__ == "__main__":
    main()
