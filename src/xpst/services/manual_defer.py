"""Deferral of manual posts outside the anti-bot window (G11: deferral != failure).

A deliberate ``xpst post`` fired at midnight used to be reported as a FAILURE
by the time-of-day gate (D2): exit code 1, error string, and nothing left
behind — the user's intent evaporated. G11 already settled the semantics for
the daemon path: deferral is scheduling, not failure. This module applies the
same rule to the manual surfaces (CLI ``post`` and the MCP ``xpst_post``
tool): when the posting window is closed, the post is QUEUED as a schedule
entry for the next window opening, and the caller is told it was deferred and
when it will fire — exit code 0, nothing lost.

A request can bypass the queue with an explicit ``force`` flag: a human who
says "post now, I know it's midnight" should be obeyed, not lectured.

A PAUSED deployment refuses the queue instead of filling it
(``schedule.posting_paused`` / ``XPST_POSTING_PAUSED``): the deferral is the
only place an implicit post becomes a schedule entry, so the guard lives here
and no surface can write one behind the operator's back. The verdict says what
happened (nothing queued, nothing uploaded) rather than pretending the post was
deferred.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from xpst.content import (
    PUBLISH_ROUTE_CAROUSEL,
    PUBLISH_ROUTE_IMAGE,
    PUBLISH_ROUTE_TEXT,
    PUBLISH_ROUTE_VIDEO,
)

if TYPE_CHECKING:
    from xpst.config import XPSTConfig
    from xpst.content import ContentRequest

#: Env spellings that turn the deployment pause on without editing config.yaml.
_TRUE_SPELLINGS = frozenset({"1", "true", "yes", "on"})


def posting_pause_reason(config: XPSTConfig) -> str:
    """Why implicit posting is paused on this deployment, or ``""``.

    Two switches, one meaning: ``schedule.posting_paused`` in config and the
    ``XPST_POSTING_PAUSED`` environment variable. The env var exists because a
    lane-level pause is applied by whoever runs the deployment, not only by
    whoever owns the config file.
    """
    if os.environ.get("XPST_POSTING_PAUSED", "").strip().lower() in _TRUE_SPELLINGS:
        return "XPST_POSTING_PAUSED is set"
    schedule = getattr(config, "schedule", None)
    if bool(getattr(schedule, "posting_paused", False)):
        return "schedule.posting_paused is true in config"
    return ""


#: Routes the schedule store can represent and therefore defer. Text is
#: deferrable too (it always was intent-wise — a midnight text was simply
#: posted at midnight): the store carries it as a content_type="text" entry
#: with an empty media path, and the fire path publishes it through
#: post_text. An earlier revision called text "never deferrable" because the
#: store had no no-media representation; that gap is closed, and parking a
#: deferred text behind a placeholder .txt is what the fire-route defect
#: (.txt into the video encoder) was born from.
_DEFERRABLE_ROUTES = frozenset(
    {PUBLISH_ROUTE_VIDEO, PUBLISH_ROUTE_IMAGE, PUBLISH_ROUTE_CAROUSEL, PUBLISH_ROUTE_TEXT}
)


def defer_manual_post_to_schedule(
    config: XPSTConfig,
    request: ContentRequest,
    route: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Queue a manual media post that the anti-bot window refuses right now.

    Returns a verdict dict when the post was queued (the caller must report it
    and treat the run as successful-but-deferred), or ``None`` when the
    request may proceed immediately (inside the window, or a request the
    store cannot represent — a media route whose media set is empty).

    The verdict is deliberately honest: ``deferred: True`` plus the created
    schedule entry (id, when it will fire), so every surface can say "deferred
    until <time>, scheduled as <id>" instead of "failed".
    """
    if route not in _DEFERRABLE_ROUTES:
        return None

    from xpst.anti_bot import AntiBotProtection

    anti_bot = AntiBotProtection()
    if anti_bot.should_post_now():
        return None

    resume = anti_bot.next_posting_time()
    # Fire a minute past the window opening so the entry is unambiguously due
    # inside the window even under clock skew.
    fire_at = resume + timedelta(minutes=1)

    # F3: a paused deployment must not be able to accumulate a queue. The
    # deferral itself — not the caller — is where an implicit post becomes a
    # schedule entry, so the guard lives here and covers every surface that
    # defers (CLI post, MCP xpst_post). Refused, not silently dropped: the
    # verdict says what happened and what to do instead, and the store is
    # never opened.
    pause_reason = posting_pause_reason(config)
    if pause_reason:
        return {
            "paused": True,
            "deferred": False,
            "reason": "posting_paused",
            "resume_after": None,
            "scheduled": None,
            "message": (
                f"Posting is paused ({pause_reason}): this post was NOT queued and "
                "nothing was uploaded. The schedule store was not written. Fire it "
                f"deliberately with --now, or clear the pause to allow deferral."
            ),
        }

    from xpst.schedule_manager import ScheduleManager

    media = [str(path) for path in request.resolved_media]
    # A media route with nothing resolved cannot be queued; a TEXT route is
    # the modality with no media by definition, so an empty media set is
    # exactly what it stores (empty path + content_type="text").
    if not media and route != PUBLISH_ROUTE_TEXT:
        return None
    manager = ScheduleManager(config.config_dir)
    entry = manager.add(
        video_path=media[0] if media else "",
        caption=request.caption,
        scheduled_time=fire_at,
        platforms=list(request.platforms) or None,
        per_platform_captions=request.per_platform_texts() or None,
        content_type=route,
        media_paths=media if len(media) > 1 else None,
    )
    return {
        "deferred": True,
        "reason": "outside_anti_bot_window",
        "resume_after": resume.isoformat(),
        "scheduled": entry,
        "message": (
            f"Deferred: outside the posting window (8am-11pm). Queued as "
            f"schedule entry {entry['id']} to fire at "
            f"{entry.get('scheduled_time_local') or entry['scheduled_time']}. "
            "Manage it with `xpst schedule`."
        ),
    }


__all__ = ["defer_manual_post_to_schedule", "posting_pause_reason"]
