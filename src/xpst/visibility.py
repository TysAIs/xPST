"""Upload-time visibility: ONE registry for who can honour it, and at what level.

``xpst post --visibility private`` used to be a scope decision disguised as a
flag. YouTube wrote ``status.privacyStatus``; every other destination accepted
the flag and published publicly, because the only thing that knew how to map a
visibility onto a provider was the YouTube uploader's own signature. That is
how "nothing gets posted publicly" became a sentence a caller could not check:
the plan said ``ready: true`` and nothing in it said *which* level would be
used, or whether the destination could honour the request at all.

This module is the registry that answers both questions, once, for every
surface — the plan/dry-run payload, the preflight service, the MCP tool, the
CLI advisories and the uploader's own receipt all read it (the same rule
``xpst.content.VIDEO_ENVELOPES`` follows for format envelopes). Never re-derive
a level per surface, and never invent a platform-specific mapping outside the
tables below.

Two policies, deliberately different:

* ``REFUSE`` — the destination cannot honour the request on the route that will
  run, and publishing the post anyway would silently produce something the
  caller did not ask for. The request is refused with a typed reason and the
  destination publishes nothing. TikTok is the case this registry exists for:
  its Direct Post API *does* have ``SELF_ONLY``, so "private" is expressible —
  but TikTok Studio's browser flow has no privacy control at all, and a
  requested-but-unavailable level must never become a public post.
* ``ADVISE`` — there is no route to this destination that could honour the
  request (X has no per-post private mode at any layer). Refusing would mean
  "you cannot post to X", so the post still publishes publicly and every plan
  and outcome names the destination as public. This is the shipped F2
  behaviour, kept as it was.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

# ── The request vocabulary ─────────────────────────────────────────────────
#: What a caller may ask for. The CLI rejects anything else at its parser
#: (exit 2) and the uploaders refuse it by name, so an unknown value can never
#: be coerced into "public".
PUBLIC = "public"
UNLISTED = "unlisted"
PRIVATE = "private"
VISIBILITY_CHOICES: tuple[str, ...] = (PUBLIC, UNLISTED, PRIVATE)

# ── Routes ─────────────────────────────────────────────────────────────────
#: A destination's documented publish endpoint.
ROUTE_API = "api"
#: TikTok's opt-in browser-native publisher (TikTok Studio's web composer).
ROUTE_TIKTOK_BROWSER = "tiktok_browser"
#: TikTok's inbox-draft upload — nothing is published by xPST; a human finishes
#: the post in the app.
ROUTE_TIKTOK_DRAFT = "inbox_draft"

# ── Policies ───────────────────────────────────────────────────────────────
REFUSE = "refuse"
ADVISE = "advise"


@dataclass(frozen=True)
class VisibilitySupport:
    """What ONE destination route can do about an upload-time visibility.

    Attributes:
        platform: destination name, lower-case.
        route: the publish route this row describes (``api``,
            ``tiktok_browser``, ``inbox_draft``).
        levels: ``{requested level: provider-native level}`` — only the levels
            this route really honours. A request missing from this map cannot
            be honoured on this route.
        default_level: the native level this route uses when nothing was
            requested (``None`` when the route has no such control).
        policy: :data:`REFUSE` or :data:`ADVISE` (see the module docstring).
        note: the sentence every surface quotes as the reason.
    """

    platform: str
    route: str
    levels: Mapping[str, str]
    default_level: str | None
    policy: str
    note: str

    def level_for(self, requested: str | None) -> str | None:
        """The provider-native level for ``requested``, or ``None`` if unsupported."""
        return self.levels.get(normalize(requested) or "")

    @property
    def requestable(self) -> tuple[str, ...]:
        """The visibility levels this route honours, in canonical order."""
        return tuple(level for level in VISIBILITY_CHOICES if level in self.levels)


_YOUTUBE_API = VisibilitySupport(
    platform="youtube",
    route=ROUTE_API,
    levels={PUBLIC: "public", UNLISTED: "unlisted", PRIVATE: "private"},
    default_level="public",
    policy=REFUSE,
    note=(
        "YouTube writes status.privacyStatus at upload time, so every level is "
        "honoured."
    ),
)

_TIKTOK_API = VisibilitySupport(
    platform="tiktok",
    route=ROUTE_API,
    levels={PUBLIC: "PUBLIC_TO_EVERYONE", PRIVATE: "SELF_ONLY"},
    default_level=None,
    policy=REFUSE,
    note=(
        "TikTok Direct Post writes post_info.privacy_level: private is SELF_ONLY "
        "and public is PUBLIC_TO_EVERYONE. TikTok has no unlisted level, and the "
        "level is validated against creator_info/query — a level the client may "
        "not use is refused, never downgraded."
    ),
)

_TIKTOK_BROWSER = VisibilitySupport(
    platform="tiktok",
    route=ROUTE_TIKTOK_BROWSER,
    levels={PUBLIC: "public"},
    default_level="public",
    policy=REFUSE,
    note=(
        "TikTok's browser-native route drives TikTok Studio's composer, which "
        "decides the audience itself: xPST can neither read nor set it, and that "
        "route publishes publicly."
    ),
)

_TIKTOK_DRAFT = VisibilitySupport(
    platform="tiktok",
    route=ROUTE_TIKTOK_DRAFT,
    levels={},
    default_level=None,
    policy=REFUSE,
    note=(
        "An inbox draft is finished by hand in the TikTok app, so xPST never "
        "publishes it and cannot honour a visibility for it."
    ),
)


def _advisory_platform(platform: str, note: str) -> VisibilitySupport:
    """A destination with no upload-time privacy control anywhere (ADVISE)."""
    return VisibilitySupport(
        platform=platform,
        route=ROUTE_API,
        levels={PUBLIC: "public"},
        default_level="public",
        policy=ADVISE,
        note=note,
    )


_NO_PRIVATE_MODE = (
    "it has no per-post private/unlisted mode on any route, so the post is "
    "PUBLIC there"
)

#: ``(platform, route) -> what that route can honour``. Named destinations in
#: :data:`xpst.content.PUBLISH_DESTINATIONS` all appear here; a destination that
#: does not (a plugin, a future adapter) is treated as ADVISE with no levels,
#: which is the honest answer for an unknown publish path.
VISIBILITY_SUPPORT: dict[tuple[str, str], VisibilitySupport] = {
    (row.platform, row.route): row
    for row in (
        _YOUTUBE_API,
        _TIKTOK_API,
        _TIKTOK_BROWSER,
        _TIKTOK_DRAFT,
        _advisory_platform("x", _NO_PRIVATE_MODE),
        _advisory_platform("instagram", _NO_PRIVATE_MODE),
        _advisory_platform("threads", _NO_PRIVATE_MODE),
        _advisory_platform("facebook", _NO_PRIVATE_MODE),
        _advisory_platform("bluesky", _NO_PRIVATE_MODE),
        _advisory_platform("rumble", "it hands the rendition off for a manual upload, so xPST does not publish it at all"),
    )
}

#: Blocker code the plan/preflight emits when a requested level cannot be
#: honoured on the route that will run.
VISIBILITY_UNAVAILABLE_CODE = "VISIBILITY_UNAVAILABLE"
#: Warning code for a destination that will publish publicly instead (ADVISE).
VISIBILITY_UNSUPPORTED_CODE = "VISIBILITY_UNSUPPORTED"
#: Canonical destination state for a destination that refused the requested
#: level. It lives here because both the registry's consumers and the route plan
#: (:data:`xpst.services.route_plan.REFUSED_VISIBILITY`) must spell it the same.
REFUSED_VISIBILITY_STATE = "refused_visibility"


def normalize(requested: str | None) -> str | None:
    """Canonical spelling of a request: lower-case, empty → ``None``.

    ``None``/``""`` mean "nothing was requested" and are NOT the same as
    ``"public"``: an unrequested post keeps the destination's own default (and,
    on TikTok, today's reactive downgrade for an unaudited client).
    """
    raw = str(requested or "").strip().lower()
    return raw or None


def validate(requested: str | None) -> str | None:
    """The refusal sentence for an unknown level, or ``None`` when it is valid."""
    value = normalize(requested)
    if value is None or value in VISIBILITY_CHOICES:
        return None
    return (
        f"{requested!r} is not a visibility: choose one of "
        f"{', '.join(VISIBILITY_CHOICES)}."
    )


def support(platform: str, *, route: str = ROUTE_API) -> VisibilitySupport | None:
    """The registry row for one destination route, or ``None`` if unknown."""
    return VISIBILITY_SUPPORT.get((str(platform or "").strip().lower(), route))


def unknown_destination_policy(platform: str, *, route: str = ROUTE_API) -> VisibilitySupport:
    """The ADVISE row used for a destination the registry does not know."""
    return _advisory_platform(
        str(platform or "").strip().lower(),
        "no upload-time visibility control is declared for this destination",
    )


def publish_route(
    config: Any | None, platform: str, *, requested: str | None = None
) -> str:
    """The route that would actually publish for this destination right now.

    Only TikTok has more than one publishing route, so this is where its
    configured ``publish_mode`` / ``draft_mode`` is translated into the route id
    the registry keys on. Every surface asks this function instead of reading
    the config itself: a plan that says "private" for a destination whose route
    cannot honour it is exactly the lie the registry exists to prevent.

    ``requested`` matters because a requested level CHANGES the route choice:
    Direct Post is the only TikTok route that can set privacy, and an unaudited
    client is allowed to post privately — so a request keeps the official route
    instead of taking the browser preference, which would refuse. Without a
    request the documented preference stands (browser first when the client is
    unaudited).
    """
    name = str(platform or "").strip().lower()
    if name != "tiktok" or config is None:
        return ROUTE_API
    account = getattr(config, "tiktok", None)
    if account is None:
        return ROUTE_API
    draft = str(getattr(account, "draft_mode", "auto") or "auto").strip().lower()
    if draft == "always":
        return ROUTE_TIKTOK_DRAFT
    mode = str(getattr(account, "publish_mode", "auto") or "auto").strip().lower()
    if mode not in (ROUTE_TIKTOK_BROWSER, "browser", "browser_only"):
        return ROUTE_API
    if mode == "browser_only":
        return ROUTE_TIKTOK_BROWSER
    # "browser": Direct Post is attempted first, so a visibility request is
    # served by the official route. Only an unrequested post takes the
    # browser-first preference (for an unaudited client).
    if normalize(requested) is not None:
        return ROUTE_API
    return ROUTE_TIKTOK_BROWSER


def refusal(platform: str, requested: str | None, *, route: str = ROUTE_API) -> str | None:
    """The sentence refusing ``requested`` on this route, or ``None`` if honoured.

    ``None``/``""`` (nothing requested) is never refused. A level the route does
    not honour is refused with the registry's own note, so the CLI, the plan,
    the MCP tool and the uploader all quote one reason.
    """
    value = normalize(requested)
    if value is None:
        return None
    if validate(value) is not None:
        return validate(value)
    row = support(platform, route=route) or unknown_destination_policy(platform, route=route)
    if value in row.levels:
        return None
    if row.policy != REFUSE:
        # An ADVISE destination cannot honour it on any route (X has no private
        # mode at all), so refusing would mean "you cannot post to X". It still
        # publishes, and :func:`advisory` is what says "public" out loud.
        return None
    return (
        f"{row.platform} cannot honour --visibility {value} on this route: "
        f"{row.note} The request is refused rather than posting publicly; "
        f"drop --visibility {value} (the post is public there) or use a route "
        f"that can honour it."
    )


def advisory(platform: str, requested: str | None, *, route: str = ROUTE_API) -> str | None:
    """The ADVISE sentence for a post that goes out publicly anyway, or ``None``.

    Empty for the default (nothing requested): every destination publishes
    publicly then, so there is nothing to warn about.
    """
    value = normalize(requested)
    if value is None:
        return None
    row = support(platform, route=route) or unknown_destination_policy(platform, route=route)
    if row.policy != ADVISE or value in row.levels:
        return None
    return (
        f"{row.platform} cannot honour --visibility {value}: {row.note}. "
        f"The post is PUBLIC there."
    )


def plan_entry(
    platform: str,
    requested: str | None,
    *,
    route: str = ROUTE_API,
    level: str | None = None,
    honoured: bool | None = None,
) -> dict[str, Any] | None:
    """The per-destination plan row for a *requested* visibility.

    ``None`` when nothing was requested, so the default payload shape is
    untouched. Otherwise it states the level that would be used and whether the
    route can honour it at all — the answer a caller previously had to infer
    from prose.

    ``honoured`` overrides the registry's static answer for a refusal that only
    the provider could settle (the client may not use the level according to
    ``creator_info/query``). Callers pass ``False`` there; the row shape stays
    this function's job.
    """
    value = normalize(requested)
    if value is None:
        return None
    row = support(platform, route=route) or unknown_destination_policy(platform, route=route)
    native = level if level is not None else row.level_for(value)
    can_honour = honoured if honoured is not None else native is not None
    return {
        "requested": value,
        "level": native,
        "honoured": can_honour,
        "route": route,
        "enforced": row.policy,
        "note": row.note if not can_honour or row.policy == ADVISE else None,
    }


def receipt_entry(
    platform: str,
    *,
    requested: str | None = None,
    level: str | None,
    route: str = ROUTE_API,
) -> dict[str, Any]:
    """The upload receipt row: the level that was actually used, and for whom.

    Always emitted (unlike :func:`plan_entry`): after a publish the answer to
    "what level did this go up at?" must exist even when nobody asked, which is
    precisely the case TikTok's creator_info discovery decides.
    """
    value = normalize(requested)
    row = support(platform, route=route) or unknown_destination_policy(platform, route=route)
    return {
        "requested": value,
        "level": level,
        "honoured": level is not None and (value is None or level == row.level_for(value)),
        "route": route,
        "source": "request" if value is not None else "creator_info",
    }


def advisories(
    targets: Sequence[str] | Iterable[str],
    requested: str | None,
    *,
    routes: Mapping[str, str] | None = None,
) -> list[str]:
    """One advisory naming every destination that will publish publicly anyway.

    ``routes`` lets a caller state the route it resolved for each destination
    (:func:`publish_route`); it defaults to the destination's API route. Empty
    for the default request, and empty when every destination honours it.
    """
    value = normalize(requested)
    if value is None:
        return []
    ignored: list[str] = []
    honouring: list[str] = []
    for platform in targets or ():
        name = str(platform or "").strip().lower()
        if not name:
            continue
        route = (routes or {}).get(name, ROUTE_API)
        note = advisory(name, value, route=route)
        if note is not None:
            ignored.append(name)
        elif refusal(name, value, route=route) is None:
            honouring.append(name)
    if not ignored:
        return []
    honoured_text = (
        f"Only {', '.join(honouring)} honours the flag."
        if honouring
        else "No destination in this request honours the flag."
    )
    return [
        f"{', '.join(ignored)} cannot honour --visibility {value}: there is no "
        f"per-post {value} mode there, so this post is PUBLIC on "
        f"{'those destinations' if len(ignored) > 1 else 'that destination'}. "
        f"{honoured_text}"
    ]


__all__ = [
    "ADVISE",
    "PRIVATE",
    "PUBLIC",
    "REFUSE",
    "ROUTE_API",
    "ROUTE_TIKTOK_BROWSER",
    "ROUTE_TIKTOK_DRAFT",
    "UNLISTED",
    "VISIBILITY_CHOICES",
    "VISIBILITY_SUPPORT",
    "VISIBILITY_UNAVAILABLE_CODE",
    "VISIBILITY_UNSUPPORTED_CODE",
    "VisibilitySupport",
    "advisories",
    "advisory",
    "normalize",
    "plan_entry",
    "publish_route",
    "receipt_entry",
    "refusal",
    "support",
    "unknown_destination_policy",
    "validate",
]
