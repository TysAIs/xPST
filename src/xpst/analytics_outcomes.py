"""Outcome-sourced analytics: what actually published, recorded vs live.

The dashboard used to aggregate whatever snapshot rows happened to exist and
render a ``0`` for a platform that had none — Tyler's original "the numbers are
skewed" complaint. This module builds the ONE report every surface renders
(HTTP, MCP, CLI, UI) so the numbers cannot disagree between surfaces:

* **Every number traces to a post the account actually owns.** Outcomes come
  from ``state.json`` ``posted_videos[*].posted_to[platform]`` — the record
  xPST writes when a publish actually returned a platform post id — and are
  intersected with the verified ownership set. Unverifiable ownership fails
  closed (no outcomes, no metrics, no totals) and unowned ids never reach a
  total.
* **Recorded vs live are explicit.** Each metric value carries
  ``metric_source``: ``"live"`` when it was fetched from the platform API
  during this run, ``"recorded"`` when it is the last snapshot xPST persisted,
  with the capture timestamp next to it. There is no third state where the UI
  shows a value without saying where it came from.
* **No data is not zero.** A platform with no metric-bearing owned post
  reports ``totals: None`` and ``has_data: False`` — rendered as "No data",
  never as a real ``0``.

This module is pure: it takes state, snapshot rows, the ownership map and an
optional live-collection result, and returns plain data. No network, no disk,
so the same function is what the tests exercise.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from xpst.analytics import (
    ANALYTICS_METRIC_FAMILIES,
    PLATFORM_METRIC_CAPABILITIES,
    _coerce_metric,
    _freshness_metadata,
)
from xpst.utils.logger import get_logger

logger = get_logger(__name__)

#: Render order for the canonical platforms. Threads is included on purpose:
#: a platform with nothing published must still show up as "no data" rather
#: than silently disappearing from the page.
PLATFORM_ORDER: tuple[str, ...] = ("youtube", "x", "instagram", "tiktok", "threads")

SOURCE_LIVE = "live"
SOURCE_RECORDED = "recorded"

#: Outcome statuses whose metrics are counted in platform totals. A deleted or
#: failed publish is still tracked (so it is visible), but its stale view count
#: must not keep inflating the aggregate.
COUNTED_STATUSES = frozenset({"published", "hidden"})


def _iso(value: Any) -> str | None:
    """Normalise a state timestamp to a string, or ``None`` when absent."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _outcome_status(info: Mapping[str, Any], error: Any) -> str:
    """Classify a recorded publish outcome from its state record.

    ``deleted`` and ``failed`` are terminal; ``hidden`` covers a post the
    platform holds as private/unlisted (state writes ``soft_hidden`` for a
    private YouTube upload); everything else published normally.
    """
    if info.get("deleted"):
        return "deleted"
    if not info.get("id") and error:
        return "failed"
    if info.get("soft_hidden") or str(info.get("visibility") or "").lower() in {"private", "unlisted"}:
        return "hidden"
    return "published"


def _metrics_from_row(row: Mapping[str, Any], platform: str) -> dict[str, int]:
    """Metric values that are actually present in a snapshot row.

    Only the platform's declared capability is exposed, and a key that is
    missing (``None`` or absent) is omitted entirely — an omitted metric is
    rendered as "not provided", never as a real zero.
    """
    allowed = set(PLATFORM_METRIC_CAPABILITIES.get(platform, ()))
    metrics: dict[str, int] = {}
    for key in ANALYTICS_METRIC_FAMILIES:
        if key not in allowed:
            continue
        value = _coerce_metric(row.get(key))
        if value is not None:
            metrics[key] = value
    return metrics


def _recipient_index(rows: Sequence[Mapping[str, Any]] | None) -> dict[tuple[str, str], Mapping[str, Any]]:
    """Index metric rows by ``(platform, post_id)``, last write wins."""
    index: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in rows or ():
        platform = str(row.get("platform") or "").lower()
        post_id = str(row.get("post_id") or "")
        if not platform or not post_id:
            continue
        index[(platform, post_id)] = row
    return index


def _source_label(data_source: str | None, staleness: str) -> str:
    """Human label for where a platform's numbers came from."""
    if data_source is None:
        return "No data"
    if data_source == SOURCE_LIVE:
        return "Live (fetched now)"
    if staleness == "stale":
        return "Recorded (stale)"
    return "Recorded"


def _platform_note(has_data: bool, checked: bool, verified: bool, outcome_count: int) -> str | None:
    """One honest sentence for a platform row that has nothing to show."""
    if has_data:
        return None
    if checked and not verified:
        return "Ownership could not be verified — no numbers are shown for this platform"
    if outcome_count and not has_data:
        return "Posts are recorded, but no metrics have been captured yet"
    return "No posts recorded for this platform"


def build_outcome_report(
    *,
    state: Mapping[str, Any],
    snapshots: Sequence[Mapping[str, Any]] = (),
    owned_ids: Mapping[str, set[str] | None] | None = None,
    live_rows: Sequence[Mapping[str, Any]] | None = None,
    platforms: Sequence[str] = PLATFORM_ORDER,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the per-post/per-platform outcome report.

    Args:
        state: Parsed ``state.json``. Only ``posted_videos`` is read.
        snapshots: Latest persisted snapshot rows per ``(platform, post_id)``
            (``AnalyticsStore.latest()`` shape). Labelled ``"recorded"``.
        owned_ids: ``{platform: verified owned post ids}``. A platform present
            with ``None`` means ownership verification was attempted and
            failed — the platform fails closed (no outcomes, no metrics, no
            totals). A platform absent from the mapping was not checked
            because it has nothing recorded to check against.
        live_rows: Rows fetched from the platform APIs during this run. Any
            ``(platform, post_id)`` present here is labelled ``"live"`` and
            takes precedence over the recorded snapshot.
        platforms: Platforms to render, in order. Platforms with no data are
            still included so the UI can say "no data" instead of dropping
            the row (or showing a zero).
        now: Injectable clock for deterministic freshness fields.

    Returns:
        ``{"generated_at", "live", "data_source", "platforms", "diagnostics"}``
        where each platform carries ``totals`` (``None`` when there is no
        data), ``data_source``, ``has_data``, freshness fields, metric
        capability, and the per-post ``outcomes`` list.
    """
    current = now or datetime.now(timezone.utc)
    ownership = owned_ids or {}
    recorded = _recipient_index(snapshots)
    live = _recipient_index(live_rows)

    posted = state.get("posted_videos") if isinstance(state, Mapping) else None
    posted = posted if isinstance(posted, Mapping) else {}

    platforms_out: dict[str, Any] = {}
    foreign_dropped: list[str] = []
    unverified: list[str] = []
    any_live = False
    any_data = False

    for platform in platforms:
        checked = platform in ownership
        owned = ownership.get(platform)
        verified = checked and owned is not None
        if checked and not verified:
            unverified.append(platform)

        outcomes: list[dict[str, Any]] = []
        dropped_foreign = 0
        dropped_unverified = 0

        for video_id, video_data in posted.items():
            if not isinstance(video_data, Mapping):
                continue
            posted_to = video_data.get("posted_to") or {}
            if not isinstance(posted_to, Mapping):
                continue
            info = posted_to.get(platform)
            if not isinstance(info, Mapping):
                continue
            errors = video_data.get("errors") or {}
            error = errors.get(platform) if isinstance(errors, Mapping) else None
            post_id = str(info.get("id") or info.get("post_id") or "").strip()
            status = _outcome_status(info, error)
            if not verified:
                dropped_unverified += 1
                continue
            if not post_id or post_id not in (owned or set()):
                dropped_foreign += 1
                foreign_dropped.append(f"{platform}:{post_id or video_id}")
                continue

            live_row = live.get((platform, post_id))
            recorded_row = recorded.get((platform, post_id))
            row = live_row if live_row is not None else recorded_row
            metric_source: str | None = None
            metrics: dict[str, int] | None = None
            captured_at: str | None = None
            if row is not None:
                candidate = _metrics_from_row(row, platform)
                if candidate:
                    metrics = candidate
                    captured_at = _iso(row.get("captured_at") or row.get("timestamp"))
                    metric_source = SOURCE_LIVE if live_row is not None else SOURCE_RECORDED
                    if metric_source == SOURCE_LIVE:
                        any_live = True
            outcomes.append(
                {
                    "post_id": post_id,
                    "video_id": str(video_id),
                    "url": info.get("url") or None,
                    "published_at": _iso(info.get("timestamp") or video_data.get("downloaded_at")),
                    "status": status,
                    "outcome_source": "publish_record",
                    "counted_in_totals": status in COUNTED_STATUSES and metrics is not None,
                    "metric_source": metric_source,
                    "captured_at": captured_at,
                    "metrics": metrics,
                }
            )

        # Owned posts with a snapshot but no local publish record (uploads made
        # outside xPST, or a state record that was pruned). They are still
        # proven owned — the id is in the verified ownership set — so their
        # recorded metrics belong in the report rather than being silently
        # dropped. Labelled so the UI can distinguish them from a publish xPST
        # itself performed.
        seen = {o["post_id"] for o in outcomes}
        for source_rows, source_label in ((live, SOURCE_LIVE), (recorded, SOURCE_RECORDED)):
            for (row_platform, row_post_id), row in source_rows.items():
                if row_platform != platform or row_post_id in seen:
                    continue
                if not verified or row_post_id not in (owned or set()):
                    continue
                candidate = _metrics_from_row(row, platform)
                if not candidate:
                    continue
                seen.add(row_post_id)
                if source_label == SOURCE_LIVE:
                    any_live = True
                outcomes.append(
                    {
                        "post_id": row_post_id,
                        "video_id": row_post_id,
                        "url": row.get("url") or None,
                        "published_at": None,
                        "status": "published",
                        "outcome_source": "snapshot",
                        "counted_in_totals": True,
                        "metric_source": source_label,
                        "captured_at": _iso(row.get("captured_at") or row.get("timestamp")),
                        "metrics": candidate,
                    }
                )

        outcomes.sort(key=lambda item: str(item.get("published_at") or ""), reverse=True)

        totals: dict[str, int] = {}
        captures: list[str] = []
        for outcome in outcomes:
            if not outcome["counted_in_totals"]:
                continue
            for key, value in (outcome["metrics"] or {}).items():
                totals[key] = totals.get(key, 0) + int(value)
            if outcome["captured_at"]:
                captures.append(str(outcome["captured_at"]))

        has_data = bool(totals)
        if has_data:
            any_data = True
        last_captured = max(captures) if captures else None
        freshness = _freshness_metadata(last_captured, current)
        capability = PLATFORM_METRIC_CAPABILITIES.get(platform, ())
        data_source = None
        if has_data:
            data_source = SOURCE_LIVE if any(o["metric_source"] == SOURCE_LIVE for o in outcomes) else SOURCE_RECORDED

        platforms_out[platform] = {
            "platform": platform,
            "ownership_verified": verified,
            "ownership_checked": checked,
            "has_data": has_data,
            "data_source": data_source,
            # The label the UI renders. Always present so a surface that only
            # reads strings cannot invent a value for an empty platform.
            "data_source_label": _source_label(data_source, str(freshness["staleness"])),
            "note": _platform_note(has_data, checked, verified, len(outcomes)),
            "posts_recorded": len(outcomes),
            "posts_with_metrics": sum(1 for o in outcomes if o["metrics"]),
            "metrics_available": sorted(set(capability)),
            "metrics_missing": sorted(set(ANALYTICS_METRIC_FAMILIES) - set(capability)),
            # None — not {} — so every consumer must render "no data" itself.
            "totals": totals if has_data else None,
            **freshness,
            "outcomes": outcomes,
            "dropped": {"unowned": dropped_foreign, "ownership_unverified": dropped_unverified},
        }

    return {
        "generated_at": current.isoformat(),
        "live": any_live,
        "data_source": SOURCE_LIVE if any_live else (SOURCE_RECORDED if any_data else None),
        "platforms": platforms_out,
        "diagnostics": {
            # Ids that were excluded because they are not in the verified
            # ownership set — a foreign channel's video, or one of this
            # account's posts that no longer exists (deleted upstream).
            "unowned_ids_excluded": foreign_dropped,
            "ownership_unverified_platforms": unverified,
        },
    }
