"""Shared no-network preflight checks for manual posting."""

from __future__ import annotations

from pathlib import Path
from typing import Any

SUPPORTED_MEDIA_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".jpg", ".jpeg", ".png"}


def credential_paths_for_platform(config: Any, platform_name: str) -> list[str]:
    """Return local credential/session files required for a destination."""
    account = getattr(config, platform_name, None)
    if account is None:
        return []
    if platform_name == "youtube":
        return [
            str(path)
            for path in (
                getattr(account, "client_secrets", "") or "",
                getattr(account, "token_file", "") or "",
            )
            if path
        ]
    if platform_name == "x":
        path = str(getattr(account, "cookies_file", "") or "")
        return [path] if path else []
    if platform_name == "instagram":
        path = str(getattr(account, "session_file", "") or "")
        return [path] if path else []
    return []


def _parse_requested_platforms(platforms: list[str] | str | None) -> list[str]:
    if platforms is None:
        return []
    raw_items = platforms.split(",") if isinstance(platforms, str) else platforms
    return [str(item).strip().lower() for item in raw_items if str(item).strip()]


def _platform_manifests(config: Any) -> dict[str, Any]:
    from xpst.platforms.base import PlatformRegistry

    PlatformRegistry.auto_discover()
    return {manifest.name: manifest for manifest in PlatformRegistry.list_manifests(config)}


def build_post_preflight(
    *,
    config: Any,
    video_path: str | Path,
    caption: str,
    platforms: list[str] | str | None = None,
    quota: Any = None,
    carousel_paths: list[str | Path] | None = None,
) -> dict[str, Any]:
    """Build the same local posting readiness payload for desktop, CLI, and MCP."""
    requested_platforms = _parse_requested_platforms(platforms)
    manifests = _platform_manifests(config)
    enabled_platforms = [
        name
        for name in ("youtube", "instagram", "x")
        if getattr(getattr(config, name, None), "enabled", False)
    ]
    targets = requested_platforms or enabled_platforms

    blocking: list[str] = []
    warnings: list[str] = []

    media_inputs = [video_path, *(carousel_paths or [])]
    media_paths = [Path(str(item)).expanduser() for item in media_inputs]
    for index, raw_path in enumerate(media_inputs):
        raw_text = str(raw_path)
        path = media_paths[index]
        label = "Video file" if index == 0 else f"Carousel item {index + 1}"
        if not raw_text.strip():
            blocking.append("Choose a video file before posting.")
            continue
        if not path.exists():
            blocking.append(f"{label} not found: {raw_text}")
            continue
        if not path.is_file():
            blocking.append(f"{label} path is not a file: {raw_text}")
            continue
        if path.stat().st_size == 0:
            blocking.append(f"{label} is empty: {raw_text}")
        if path.suffix.lower() and path.suffix.lower() not in SUPPORTED_MEDIA_SUFFIXES:
            warnings.append(f"{path.suffix.lower()} may need conversion before upload.")

    if not caption.strip():
        warnings.append("Caption is empty.")

    if not targets:
        blocking.append("Enable at least one destination platform.")

    platform_previews: list[dict[str, Any]] = []
    for platform_name in targets:
        if platform_name not in manifests:
            blocking.append(f"{platform_name} is not an installed destination provider.")
            continue

        account = getattr(config, platform_name, None)
        enabled = bool(getattr(account, "enabled", False))
        manifest = manifests[platform_name]
        extra = manifest.extra
        platform_warnings: list[str] = []
        platform_blocking: list[str] = []

        if not enabled:
            platform_blocking.append(f"{manifest.display_name} is disabled.")

        credential_paths = credential_paths_for_platform(config, platform_name)
        if credential_paths and any(
            not Path(credential_path).expanduser().exists()
            for credential_path in credential_paths
        ):
            platform_blocking.append(f"{manifest.display_name} is not connected.")

        max_caption = extra.get("max_caption_length")
        if isinstance(max_caption, int) and len(caption) > max_caption:
            platform_blocking.append(
                f"{manifest.display_name} caption is {len(caption) - max_caption} character(s) too long."
            )

        if quota is not None:
            try:
                if not quota.can_upload(platform_name):
                    platform_blocking.append(f"{manifest.display_name} daily quota is exhausted.")
            except Exception:
                platform_warnings.append(f"{manifest.display_name} quota could not be checked.")

        platform_previews.append(
            {
                "name": platform_name,
                "display_name": manifest.display_name,
                "auth_mode": manifest.auth_mode.value,
                "official": manifest.is_official_api,
                "max_caption_length": max_caption,
                "caption_length": len(caption),
                "blocking": platform_blocking,
                "warnings": platform_warnings,
                "ready": not platform_blocking,
            }
        )
        blocking.extend(platform_blocking)
        warnings.extend(platform_warnings)

    first_path = media_paths[0] if media_paths else Path("")
    file_size = first_path.stat().st_size if first_path.exists() and first_path.is_file() else 0
    return {
        "ok": True,
        "ready": not blocking,
        "video": {
            "path": str(first_path),
            "filename": first_path.name,
            "size_bytes": file_size,
            "size_mb": round(file_size / (1024 * 1024), 2) if file_size else 0,
        },
        "caption": caption,
        "caption_length": len(caption),
        "carousel": len(media_paths) > 1,
        "items": len(media_paths),
        "targets": targets,
        "platforms": platform_previews,
        "blocking": blocking,
        "warnings": warnings,
    }
