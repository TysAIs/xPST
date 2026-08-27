"""Registry-correctness regression tests (audit wave 1).

Guards against the duplicate-registration bug where ``PlatformRegistry.auto_discover``
registered every ``PlatformUploader`` subclass under its mangled class name IN
ADDITION to the explicit module-level ``register()`` calls — exposing
``messenger`` AND ``messengeradapter`` for the same physical platform (7 registry
entries for 6 platforms).

Post-fix contract:
- Each physical platform appears exactly once, under its canonical name:
  youtube, x, instagram, tiktok, threads, messenger.
- auto-discovery never produces duplicate keys and is idempotent.
- The legacy name ``messengeradapter`` remains resolvable as a backward-compat
  alias (no data migration needed) but is never listed as a separate key.
"""

from __future__ import annotations

from xpst.config import XPSTConfig
from xpst.platforms.base import PlatformRegistry
from xpst.platforms.messenger import MessengerAdapter

# The six canonical destination platforms. A physical platform may appear
# exactly once, under its canonical key.
CANONICAL_PLATFORMS = {"youtube", "x", "instagram", "tiktok", "threads", "messenger"}


def _fresh_registry() -> dict:
    """Return a fresh, unpopulated registry and install it as the active one."""
    original = PlatformRegistry._registry
    PlatformRegistry._registry = {}
    return original


def test_auto_discover_registers_each_canonical_platform_exactly_once() -> None:
    """auto_discover must yield exactly the 6 canonical keys, with no duplicates."""
    original = _fresh_registry()
    try:
        # Run discovery as startup code does. Running it twice must be
        # idempotent — a second pass must not re-register anything.
        PlatformRegistry.auto_discover()
        PlatformRegistry.auto_discover()

        names = PlatformRegistry.list_platforms()
        assert len(names) == len(set(names)), f"duplicate keys produced: {names}"
        assert set(names) == CANONICAL_PLATFORMS, f"unexpected registry keys: {names}"
        assert "messengeradapter" not in names
        assert names.count("messenger") == 1
    finally:
        PlatformRegistry._registry = original


def test_auto_discover_skips_explicitly_registered_subclasses() -> None:
    """Explicit register() calls stay the source of truth after auto-discovery.

    Simulates real runtime: built-in modules are imported (running their explicit
    module-level register() calls) and THEN auto_discover runs — it must not add
    mangled duplicate keys on top.
    """
    original = _fresh_registry()
    try:
        # Mirror the module-level register() calls (source of truth), in the
        # same order the modules declare them.
        from xpst.platforms.instagram import InstagramUploader
        from xpst.platforms.threads import ThreadsUploader
        from xpst.platforms.tiktok import TikTokUploader
        from xpst.platforms.x import XUploader
        from xpst.platforms.youtube import YouTubeUploader

        PlatformRegistry.register("youtube", YouTubeUploader)
        PlatformRegistry.register("x", XUploader)
        PlatformRegistry.register("threads", ThreadsUploader)
        PlatformRegistry.register("instagram", InstagramUploader)
        PlatformRegistry.register("tiktok", TikTokUploader)
        PlatformRegistry.register("messenger", MessengerAdapter)

        # Run auto_discover on top (twice — must be idempotent).
        PlatformRegistry.auto_discover()
        PlatformRegistry.auto_discover()

        names = PlatformRegistry.list_platforms()
        assert len(names) == 6, f"expected 6 canonical platforms, got {len(names)}"
        assert set(names) == CANONICAL_PLATFORMS, f"unexpected registry keys: {names}"
        assert "messengeradapter" not in names, "mangled messengeradapter key leaked back in"
    finally:
        PlatformRegistry._registry = original


def test_messengeradapter_legacy_alias_stays_resolvable() -> None:
    """'messengeradapter' must not break existing data: alias, not migration.

    Legacy user data (config/state) may refer to ``messengeradapter``; it must
    resolve to the messenger platform and never surface as a separate key.
    """
    original = _fresh_registry()
    try:
        PlatformRegistry.register("messengeradapter", MessengerAdapter)

        # The alias collapses into the canonical key — no duplicate entry.
        names = PlatformRegistry.list_platforms()
        assert names == ["messenger"]
        assert "messengeradapter" not in names

        # Legacy read path resolves to the messenger adapter.
        adapter = PlatformRegistry.get("messengeradapter", XPSTConfig())
        assert isinstance(adapter, MessengerAdapter)
        assert adapter.platform_name == "messenger"

        # Canonical read path returns the same platform.
        canonical = PlatformRegistry.get("messenger", XPSTConfig())
        assert type(canonical) is type(adapter)

        # Unknown names still raise the usual KeyError.
        try:
            PlatformRegistry.get("nope", XPSTConfig())
        except KeyError:
            pass
        else:  # pragma: no cover
            raise AssertionError("expected KeyError for unknown platform")
    finally:
        PlatformRegistry._registry = original


def test_platform_registry_never_exposes_duplicate_classes() -> None:
    """No class may be registered under more than one key."""
    original = _fresh_registry()
    try:
        PlatformRegistry.auto_discover()
        seen_classes = set(PlatformRegistry._registry.values())
        assert len(seen_classes) == len(PlatformRegistry._registry), (
            "the same uploader class is registered under multiple keys"
        )
        assert set(PlatformRegistry._registry) == CANONICAL_PLATFORMS
    finally:
        PlatformRegistry._registry = original
