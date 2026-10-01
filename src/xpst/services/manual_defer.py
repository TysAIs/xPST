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
"""

from __future__ import annotations

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


__all__ = ["defer_manual_post_to_schedule"]
