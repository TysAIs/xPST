"""Outcome-sourced analytics: what actually published, recorded vs live.

The dashboard used to aggregate whatever snapshot rows happened to exist and
render a ``0`` for a platform that had none — Alex's original "the numbers are
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
    REASON_POST_NOT_FOUND_UPSTREAM,
    REASON_TWIKIT_DETAIL_SHAPE,
    _coerce_metric,
    _freshness_metadata,
)
from xpst.analytics_store import (
    COLLECTION_STATUS_FAILED,
    COLLECTION_STATUS_UNKNOWN,
    normalise_collection_status,
)
from xpst.state_schema import resolve_platform_post_id
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
    if not resolve_platform_post_id(info) and error:
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


#: Human wording for a collection outcome. ``unknown`` is not "ok": a snapshot
#: written before collection status was tracked cannot claim its numbers came
#: from a successful fetch.
_COLLECTION_STATUS_LABELS = {
    "ok": "Collection ok",
    "partial": "Collection partial (some posts failed)",
    "failed": "Collection failed",
    "unavailable": "Collection unavailable",
    "not_collected": "Not collected",
    COLLECTION_STATUS_UNKNOWN: "Collection status unknown",
}


def collection_status_label(status: Any) -> str:
    """Human wording for a collection status value (never blank)."""
    return _COLLECTION_STATUS_LABELS.get(normalise_collection_status(status), _COLLECTION_STATUS_LABELS[COLLECTION_STATUS_UNKNOWN])


def _derive_collection_status(
    *,
    status_block: Mapping[str, Any] | None,
    collection_error: Mapping[str, Any] | None,
    unavailable: Mapping[str, Any] | None,
) -> tuple[str, str | None, str | None, str | None]:
    """Resolve one platform's collection status: ``(status, source, error, at)``.

    Priority is live evidence, then the persisted ledger, then ``unknown``.
    Anything unlabelled stays ``unknown`` — the module never upgrades missing
    evidence to ``ok``.
    """
    if status_block and status_block.get("status"):
        status = normalise_collection_status(status_block.get("status"))
        source = str(status_block.get("source") or "") or None
        if source is None and status != COLLECTION_STATUS_UNKNOWN:
            source = "live" if collection_error else "recorded"
        if status == COLLECTION_STATUS_UNKNOWN and not collection_error:
            # No live failure and no known status: nothing to attribute.
            source = None
        error = status_block.get("error")
        recorded_at = _iso(status_block.get("recorded_at") or status_block.get("captured_at"))
        return status, source, str(error) if error else None, recorded_at
    if collection_error and collection_error.get("failures"):
        requested = int(collection_error.get("requested") or 0)
        collected = int(collection_error.get("collected") or 0)
        status = "partial" if (requested and collected) else COLLECTION_STATUS_FAILED
        error = collection_error.get("error")
        return status, "live", str(error) if error else None, None
    if unavailable:
        return "unavailable", "live", None, None
    return COLLECTION_STATUS_UNKNOWN, None, None, None


def _platform_note(
    has_data: bool,
    checked: bool,
    verified: bool,
    outcome_count: int,
    collection_error: Mapping[str, Any] | None = None,
    unavailable: Mapping[str, Any] | None = None,
    *,
    collection_status: str | None = None,
    collection_status_source: str | None = None,
    recorded_error: str | None = None,
) -> str | None:
    """One honest sentence for a platform row that has nothing to show.

    QA-2026-09-28 D1: when the live collection FAILED this run, say so —
    "no metrics have been captured yet" reads like a young account when the
    collector actually broke, which is the silent-zero defect class.

    A platform that answered definitively (its recorded posts no longer exist
    upstream, or the provider's response shape is unreadable) names that reason
    instead of implying a young account or a broken collector.

    Issue #297: the same honesty is owed to numbers that were persisted during
    a failed collection. A recorded row whose status is ``failed`` (or which
    was never labelled at all) keeps its numbers visible but says they are not
    current measurements.
    """
    status = normalise_collection_status(collection_status) if collection_status else None
    if collection_error and collection_error.get("failures"):
        err = str(collection_error.get("error") or "collection error")
        failed = collection_error.get("requested") or collection_error.get("failures")
        kept = " Keeping previously recorded numbers." if has_data else ""
        return f"Collection failed for {failed} post(s): {err}.{kept}"
    if status == COLLECTION_STATUS_FAILED and collection_status_source == "recorded":
        err = str(recorded_error or "collection error")
        if has_data:
            return (
                f"Recorded numbers are not current: the last collection attempt failed "
                f"({err})."
            )
        return f"Collection failed on the last attempt: {err}."
    if unavailable and has_data:
        reason = str(unavailable.get("reason") or "unavailable")
        if reason == REASON_POST_NOT_FOUND_UPSTREAM:
            posts = unavailable.get("posts")
            return (
                f"Kept recorded numbers — {posts} recorded post(s) no longer exist "
                f"upstream ({reason}), so this run had nothing to refresh"
            )
        return f"Kept recorded numbers — collection unavailable ({reason})"
    if unavailable and not has_data:
        reason = str(unavailable.get("reason") or "unavailable")
        detail = str(unavailable.get("detail") or "").strip()
        if reason == REASON_POST_NOT_FOUND_UPSTREAM:
            posts = unavailable.get("posts")
            return (
                f"{posts if posts is not None else ''} recorded post(s) no longer exist "
                f"upstream ({reason}) — nothing to collect, not a collection failure"
            ).strip()
        if reason == REASON_TWIKIT_DETAIL_SHAPE:
            return (
                "X's TweetDetail response shape is unreadable "
                f"({reason}) — analytics unavailable until the parser is updated"
            )
        return f"Collection unavailable ({reason}){': ' + detail if detail else ''}"
    if has_data:
        if status == COLLECTION_STATUS_UNKNOWN:
            return (
                "Recorded numbers carry no collection status (unknown) — whether the "
                "capture completed cannot be confirmed."
            )
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
    collection_errors: Mapping[str, Mapping[str, Any]] | None = None,
    unavailable: Mapping[str, Mapping[str, Any]] | None = None,
    collection_status: Mapping[str, Mapping[str, Any]] | None = None,
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
        collection_errors: Per-platform failure records (``failures`` > 0 is a
            real failure).
        unavailable: Per-platform definitive answers,
            ``{platform: {"reason", "detail", "posts", ...}}`` — "there is
            nothing to collect and here is why", which is not a failure.
        collection_status: Per-platform collection outcomes
            (``{platform: {"status", "source", "error", "recorded_at"}}``),
            typically the live run's status merged with the persisted ledger.
            A platform absent from this mapping is reported as ``unknown`` —
            never as a successful collection.

    Returns:
        ``{"generated_at", "live", "data_source", "platforms", "diagnostics"}``
        where each platform carries ``totals`` (``None`` when there is no
        data), ``data_source``, ``has_data``, freshness fields, metric
        capability, the per-post ``outcomes`` list, and ``unavailable`` (a
        reason-bearing mapping, or ``None``).
    """
    current = now or datetime.now(timezone.utc)
    ownership = owned_ids or {}
    collect_errors = collection_errors or {}
    unavailable_map = unavailable or {}
    status_map = collection_status or {}
    recorded = _recipient_index(snapshots)
    live = _recipient_index(live_rows)

    posted = state.get("posted_videos") if isinstance(state, Mapping) else None
    posted = posted if isinstance(posted, Mapping) else {}

    platforms_out: dict[str, Any] = {}
    foreign_dropped: list[str] = []
    unverified: list[str] = []
    collection_failed: list[str] = []
    unavailable_platforms: dict[str, Any] = {}
    unknown_collection_platforms: list[str] = []
    untrusted_recorded_platforms: list[str] = []
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
            post_id = resolve_platform_post_id(info)
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
            row_collection_status: str | None = None
            if row is not None:
                candidate = _metrics_from_row(row, platform)
                if candidate:
                    metrics = candidate
                    captured_at = _iso(row.get("captured_at") or row.get("timestamp"))
                    metric_source = SOURCE_LIVE if live_row is not None else SOURCE_RECORDED
                    row_collection_status = (
                        "ok"
                        if live_row is not None
                        else normalise_collection_status(row.get("collection_status"))
                    )
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
                    "counted_in_totals": (
                        status in COUNTED_STATUSES
                        and metrics is not None
                        # A number recorded during a FAILED collection is not a
                        # measurement; keep it visible, out of the totals.
                        and row_collection_status != COLLECTION_STATUS_FAILED
                    ),
                    "metric_source": metric_source,
                    "collection_status": row_collection_status,
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
                outcome_status = (
                    "ok" if source_label == SOURCE_LIVE else normalise_collection_status(row.get("collection_status"))
                )
                outcomes.append(
                    {
                        "post_id": row_post_id,
                        "video_id": row_post_id,
                        "url": row.get("url") or None,
                        "published_at": None,
                        "status": "published",
                        "outcome_source": "snapshot",
                        "counted_in_totals": outcome_status != COLLECTION_STATUS_FAILED,
                        "metric_source": source_label,
                        "collection_status": outcome_status,
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
        collection_error = collect_errors.get(platform) or None
        if collection_error and collection_error.get("failures"):
            collection_failed.append(platform)
        platform_unavailable = unavailable_map.get(platform) or None
        if platform_unavailable:
            unavailable_platforms[platform] = {
                "reason": platform_unavailable.get("reason"),
                "posts": platform_unavailable.get("posts"),
                "detail": platform_unavailable.get("detail"),
            }
        (
            platform_collection_status,
            collection_status_source,
            recorded_collection_error,
            last_collection_attempt_at,
        ) = _derive_collection_status(
            status_block=status_map.get(platform),
            collection_error=collection_error,
            unavailable=platform_unavailable,
        )
        if (
            platform_collection_status == COLLECTION_STATUS_FAILED
            and collection_status_source == "live"
            and platform not in collection_failed
        ):
            # Exit-code semantics stay scoped to THIS run: a failure recorded
            # days ago must not make an unrelated today's run exit non-zero.
            collection_failed.append(platform)
        if platform_collection_status == COLLECTION_STATUS_UNKNOWN:
            unknown_collection_platforms.append(platform)
        elif platform_collection_status == COLLECTION_STATUS_FAILED and collection_status_source == "recorded":
            untrusted_recorded_platforms.append(platform)

        platforms_out[platform] = {
            "platform": platform,
            "ownership_verified": verified,
            "ownership_checked": checked,
            "has_data": has_data,
            "data_source": data_source,
            # The label the UI renders. Always present so a surface that only
            # reads strings cannot invent a value for an empty platform.
            "data_source_label": _source_label(data_source, str(freshness["staleness"])),
            "note": _platform_note(
                has_data,
                checked,
                verified,
                len(outcomes),
                collection_error,
                platform_unavailable,
                collection_status=platform_collection_status,
                collection_status_source=collection_status_source,
                recorded_error=recorded_collection_error,
            ),
            # Collection provenance (issue #297). Always present: a surface
            # must be able to say "unknown" instead of implying success for a
            # row whose capture outcome was never recorded.
            "collection_status": platform_collection_status,
            "collection_status_label": collection_status_label(platform_collection_status),
            "collection_status_source": collection_status_source,
            "last_collection_attempt_at": last_collection_attempt_at,
            "recorded_collection_error": recorded_collection_error,
            # Structured mirror of the note for machine consumers: None when
            # collection succeeded (or was not attempted); a {failures,
            # error, ...} mapping when posts could not be fetched. A row with
            # totals==null AND collection_error set is a FAILURE, not a zero.
            "collection_error": (
                {k: v for k, v in collection_error.items() if v is not None}
                if collection_error and collection_error.get("failures")
                else None
            ),
            # Definitive "nothing to collect" answer with its machine reason
            # (e.g. every recorded post was deleted upstream). Not a failure.
            "unavailable": (
                {
                    "reason": platform_unavailable.get("reason"),
                    "detail": platform_unavailable.get("detail"),
                    "posts": platform_unavailable.get("posts"),
                }
                if platform_unavailable
                else None
            ),
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
            # Platforms whose live collection raised this run — their rows
            # are failures, not zeros (QA-2026-09-28 D1 silent-zero class).
            "collection_failed_platforms": collection_failed,
            # Issue #297: platforms whose recorded numbers were written
            # without any collection status (legacy rows). Their values are
            # readable but must not be reported as a successful collection.
            "unknown_collection_status_platforms": unknown_collection_platforms,
            # Platforms whose last recorded collection attempt FAILED, so the
            # recorded numbers shown for them are not current measurements.
            "untrusted_recorded_platforms": untrusted_recorded_platforms,
            # Platforms that answered "there is nothing to collect" with a
            # reason ({platform: {reason, posts, detail}}). This is the honest
            # sibling of collection_failed_platforms: same missing numbers,
            # opposite meaning, so a consumer must not treat them alike.
            "unavailable_platforms": unavailable_platforms,
        },
    }
