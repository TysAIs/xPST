#!/usr/bin/env python3
"""Capture the Python dashboard HTTP surface as JSON fixtures.

The Rust `xpst-api` crate must not guess what the Python dashboard serves:
this script imports the REAL FastAPI app, enumerates its routes, and records a
sample payload for every route the Rust surface claims to implement. The
fixtures it writes are what `crates/xpst-api/tests/route_parity.rs` compares
against, so "the Rust router matches Python" is a checked fact rather than a
transcription.

Usage (from the repo root):

    PYTHONPATH="$PWD/src" XPST_PYTHON=<venv>/bin/python \
        cargo xtask routes --write

Two fixtures are written:

* ``crates/xpst-api/src/parity/route_parity.json``      — every route
* ``crates/xpst-api/src/parity/payload_shapes.json``    — sample payloads
* ``crates/xpst-api/src/parity/pairing_refusals.json``  — pairing refusals

Nothing here touches the network: the config dir is an empty temporary
directory, so no account has a credential to probe.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any


def _route_rows(app: Any) -> list[dict[str, Any]]:
    """Every route the app answers, classified, without duplicates."""
    from fastapi.routing import APIRoute
    from starlette.routing import Mount

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()

    def add(path: str, methods: list[str], name: str, kind: str) -> None:
        key = (path, tuple(sorted(methods)))
        if key in seen:
            return
        seen.add(key)
        rows.append(
            {
                "path": path,
                "methods": sorted(m for m in methods if m not in {"HEAD", "OPTIONS"}),
                "name": name,
                "kind": kind,
            }
        )

    # Routes the app itself declares (health, state, bio, static mount …).
    for route in app.routes:
        if isinstance(route, APIRoute):
            kind = "floor" if not route.path.startswith("/api") else "api"
            if route.path in _DEV_DOC_PATHS:
                kind = "dev_doc"
            add(route.path, list(route.methods or []), route.name, kind)
        elif isinstance(route, Mount):
            add(
                str(getattr(route, "path", "/")),
                ["GET"],
                str(getattr(route, "name", "") or "mount"),
                "static_mount",
            )

    # The /api router is included through an indirection the app object does not
    # flatten, so enumerate it directly — that is the surface under test.
    from xpst.dashboard.api import create_api_router

    for route in create_api_router(temp_config_dir()).routes:
        add(route.path, list(route.methods or []), route.name, "api")

    return rows


_DEV_DOC_PATHS = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


def temp_config_dir() -> str:
    return os.environ["XPST_CAPTURE_CONFIG_DIR"]


SAMPLES: list[tuple[str, str, str, Any]] = [
    ("GET", "/health", "health", None),
    ("GET", "/state", "state", None),
    ("GET", "/api/providers", "api.providers", None),
    ("GET", "/api/capabilities", "api.capabilities", None),
    ("GET", "/api/settings", "api.settings", None),
    ("GET", "/api/health-status", "api.health_status", None),
    ("GET", "/api/schedules", "api.schedules", None),
    ("GET", "/api/devices", "api.devices", None),
    ("POST", "/api/pair", "api.pair.missing_code", {"code": ""}),
    (
        "POST",
        "/api/preflight",
        "api.preflight",
        {"media_paths": [], "caption": "hi", "platforms": ["youtube"]},
    ),
    ("POST", "/api/plans", "api.plans.create_error", {"caption": "hi"}),
    ("GET", "/api/plans/plan_missing", "api.plans.get_error", None),
    ("GET", "/api/summary", "api.summary", None),
]


def capture_payloads(app: Any) -> dict[str, Any]:
    from fastapi.testclient import TestClient

    token = os.environ["XPST_API_TOKEN"]
    client = TestClient(app, raise_server_exceptions=False)
    captured: dict[str, Any] = {}
    for method, path, route_id, body in SAMPLES:
        kwargs: dict[str, Any] = {"headers": {"Authorization": f"Bearer {token}"}}
        if body is not None:
            kwargs["json"] = body
        try:
            response = client.request(method, path, **kwargs)
        except Exception as exc:  # noqa: BLE001 - a sample that cannot run is recorded as such
            captured[route_id] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        payload: Any = response.text
        if response.headers.get("content-type", "").startswith("application/json"):
            try:
                payload = response.json()
            except ValueError:
                payload = {"<unparseable>": response.text[:200]}
        captured[route_id] = {"status": response.status_code, "body": payload}
    return captured


#: Values that differ between two captures of the same surface (wall clock,
#: temporary directory). They are replaced so re-running the capture reproduces
#: the fixture byte-for-byte; the parity test compares key paths, so no asserted
#: fact is lost.
VOLATILE_KEYS = frozenset(
    {
        "config_dir",
        "generated_at",
        "generated_at_iso",
        "auth_checked_at",
        "auth_checked_at_iso",
        "auth_age_seconds",
        "checked_at",
        "created_at",
        "updated_at",
        "last_seen_at",
        "now_ms",
        "timestamp",
        "expires_at",
        "last_check",
        "last_captured",
        "last_captured_relative",
        "freshness",
    }
)


def scrub(value: Any, workdir: str) -> Any:
    """Replace volatile values and the temporary directory path."""
    if isinstance(value, dict):
        return {
            key: (
                scrub(item, workdir)
                if key not in VOLATILE_KEYS
                else "<volatile>"
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [scrub(item, workdir) for item in value]
    if isinstance(value, str):
        scrubbed = value.replace(workdir, "<config-dir>")
        # Messages quote paths from other config dirs on the capture machine;
        # the user segment is never part of the fixture.
        return re.sub(r"(/home/|/Users/)[^/\s\"']+", r"\1<user>", scrubbed)
    if isinstance(value, float) and value > 1_000_000:
        # An epoch timestamp that leaked outside a named field.
        return 0.0
    return value


def capture_pairing_refusals() -> dict[str, Any]:
    """Every refusal the pairing exchange can produce, from the real code."""
    from xpst.dashboard.devices import (
        PairingError,
        create_pairing_code,
        redeem_pairing_code,
    )

    rows: dict[str, Any] = {}
    directory = temp_config_dir()

    def record(name: str, fn: Any) -> None:
        try:
            fn()
        except PairingError as exc:
            rows[name] = {
                "code": exc.code,
                "message": exc.message,
                "status_code": exc.status_code,
            }
        else:
            rows[name] = {"code": "<no-refusal>", "message": "", "status_code": 200}

    def unknown() -> None:
        redeem_pairing_code(directory, "ZZZZZZZZ")

    record("unknown", unknown)

    def expired() -> None:
        issued = create_pairing_code(directory, code="EXPIRE22", now=1_000.0)
        assert issued["code"] == "EXPI-RE22", issued
        redeem_pairing_code(directory, "EXPIRE22", now=1_000.0 + 10_000)

    record("expired", expired)

    def spent() -> None:
        create_pairing_code(directory, code="SPENT222", now=2_000.0)
        redeem_pairing_code(directory, "SPENT222", now=2_000.0)
        redeem_pairing_code(directory, "SPENT222", now=2_000.0)

    record("spent", spent)

    def short_code() -> None:
        create_pairing_code(directory, code="ABC", now=3_000.0)

    record("shape", short_code)

    def too_many() -> None:
        create_pairing_code(directory, code="BUDGET22", now=4_000.0)
        for index in range(5):
            try:
                redeem_pairing_code(directory, f"WRONG{index:03d}", now=4_000.0 + index)
            except PairingError:
                pass
        redeem_pairing_code(directory, "BUDGET22", now=4_000.0 + 6)

    record("too_many_attempts", too_many)
    return rows


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    override = os.environ.get("XPST_CAPTURE_OUT")
    out = Path(override) if override else root / "crates/xpst-api/src/parity"
    out.mkdir(parents=True, exist_ok=True)

    workdir = tempfile.mkdtemp(prefix="xpst-route-capture-")
    os.environ["XPST_CAPTURE_CONFIG_DIR"] = workdir
    # No live account probes: the capture must be deterministic and offline.
    os.environ["XPST_DISABLE_AUTH_WARM"] = "1"
    os.environ.setdefault("XPST_API_TOKEN", "capture-token-not-a-secret")
    try:
        from xpst.dashboard.server import _create_app

        app = _create_app(workdir, require_read_auth=False)
        routes = {"captured_from": "src/xpst/dashboard/server.py::_create_app", "routes": _route_rows(app)}
        payloads = {
            "captured_from": "src/xpst/dashboard/",
            "samples": scrub(capture_payloads(app), workdir),
        }
        refusals = {"captured_from": "src/xpst/dashboard/devices.py", "refusals": capture_pairing_refusals()}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    files = {
        out / "route_parity.json": routes,
        out / "payload_shapes.json": payloads,
        out / "pairing_refusals.json": refusals,
    }
    for path, document in files.items():
        path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {path} ({len(document)} keys)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
