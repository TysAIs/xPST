"""Canonical state.json keys for a published platform post id.

``state.json`` stores one destination record per published post::

    "posted_videos": {"<video_id>": {"posted_to": {"<platform>": {
        "id": "<platform post id>", "url": "...", "timestamp": "...",
    }}}}

``id`` is the canonical key: ``StateManager`` only ever writes that spelling.
Older and hand-edited state files used ``post_id`` instead. A reader that
assumed one spelling silently lost the other's posts — the defect class behind
"the live analytics refresh collects nothing" (a destination recorded with the
legacy key never yielded a post id, so nothing was ever fetched for it).

Reads therefore go through :func:`resolve_platform_post_id`, which accepts both
spellings (canonical first), and writes use :data:`CANONICAL_POST_ID_KEY`
explicitly so the canonical spelling cannot drift by accident.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: Key ``StateManager`` writes for a published platform post id.
CANONICAL_POST_ID_KEY = "id"

#: Key older / hand-edited state files used. Accepted on read only.
LEGACY_POST_ID_KEY = "post_id"

#: Accepted spellings, canonical first.
POST_ID_KEYS: tuple[str, ...] = (CANONICAL_POST_ID_KEY, LEGACY_POST_ID_KEY)


def resolve_platform_post_id(info: Mapping[str, Any] | None) -> str:
    """Return the platform post id from a ``posted_to[platform]`` record.

    Accepts the canonical ``id`` key and the legacy ``post_id`` fallback, so a
    state file written before the rename keeps working. Returns ``""`` when the
    record carries no id (a failed upload, or a structurally invalid entry) —
    callers must treat that as "nothing to fetch", never as a value.
    """
    if not isinstance(info, Mapping):
        return ""
    for key in POST_ID_KEYS:
        value = info.get(key)
        if value:
            return str(value).strip()
    return ""
