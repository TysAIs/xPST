"""Credential-aware live platform smoke checks for release owners."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xpst.config import XPSTConfig
from xpst.engine import CrossPostEngine

DESTINATION_PLATFORMS = ("youtube", "instagram", "x")


@dataclass
class PlatformCredential:
    platform: str
    configured: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "configured": self.configured,
            "reason": self.reason,
        }


def _path_exists(value: str | None) -> bool:
    return bool(value and Path(value).expanduser().exists())


def credential_status(config: XPSTConfig) -> dict[str, PlatformCredential]:
    """Return whether each destination appears credential-ready."""
    youtube_configured = _path_exists(config.youtube.client_secrets) and _path_exists(config.youtube.token_file)
    instagram_configured = _path_exists(config.instagram.session_file)
    x_configured = _path_exists(config.x.cookies_file)

    return {
        "youtube": PlatformCredential(
            "youtube",
            youtube_configured,
            "client_secrets and token_file exist" if youtube_configured else "missing YouTube OAuth files",
        ),
        "instagram": PlatformCredential(
            "instagram",
            instagram_configured,
            "session_file exists" if instagram_configured else "missing Instagram session file",
        ),
        "x": PlatformCredential(
            "x",
            x_configured,
            "cookies_file exists" if x_configured else "missing X cookies file",
        ),
    }


async def _run_health(config: XPSTConfig) -> dict[str, Any]:
    engine = CrossPostEngine(config)
    return await engine.check_health()


def verify_live_platforms(config_path: str | None = None, require: bool = False) -> dict[str, Any]:
    """Run health checks for credential-ready live platforms.

    Without ``require``, missing owner credentials are reported as skipped. With
    ``require``, skipped or failed live platforms block the result.
    """
    config = XPSTConfig.load(config_path)
    credentials = credential_status(config)
    health_data = asyncio.run(_run_health(config))
    platform_health = health_data.get("platforms", {})

    results: list[dict[str, Any]] = []
    for platform in DESTINATION_PLATFORMS:
        credential = credentials[platform]
        if not credential.configured:
            results.append({
                "platform": platform,
                "status": "skipped",
                "ok": not require,
                "reason": credential.reason,
            })
            continue

        health = platform_health.get(platform, {})
        authenticated = bool(health.get("authenticated"))
        session_valid = bool(health.get("session_valid"))
        ok = authenticated and session_valid
        results.append({
            "platform": platform,
            "status": "passed" if ok else "failed",
            "ok": ok,
            "authenticated": authenticated,
            "session_valid": session_valid,
            "error": health.get("error", ""),
            "details": health.get("details", {}),
        })

    blocking = [result for result in results if not result["ok"]]
    return {
        "ok": not blocking,
        "mode": "required" if require else "optional",
        "credentials": {name: status.to_dict() for name, status in credentials.items()},
        "results": results,
        "blocking": blocking,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run credential-aware live platform health smoke checks")
    parser.add_argument("--config", default=None, help="Optional config file path")
    parser.add_argument("--require", action="store_true", help="Fail when owner credentials are missing or invalid")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args()

    result = verify_live_platforms(args.config, require=args.require)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    else:
        for item in result["results"]:
            print(f"{item['platform']}: {item['status']}")
            if item.get("reason"):
                print(f"  {item['reason']}")
            if item.get("error"):
                print(f"  {item['error']}")

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
