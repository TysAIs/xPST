#!/usr/bin/env python3
"""Cross-implementation probes for the Rust `state.json` layer.

Every mode here drives the REAL Python engine — ``xpst.state_manager
.StateManager`` and ``xpst.state_store.StateStore`` — not a re-implementation of
its behaviour. The question this script answers is the only one that matters:
can the shipped engine read a state file Rust wrote, and does Rust read the
engine's file without losing or inventing anything?

Modes
-----
create <dir>              run the canonical scenario with Python's StateManager
dump <state.json>         canonical JSON of the whole document
dump-normalized <file>    canonical JSON with every timestamp replaced by <ts>
verify <dir>              assert the document's semantics through the real API
hold-lock <dir> <secs>    hold the engine's `.state.lock` for N seconds

Safety: refuses to run unless ``XPST_CONFIG_DIR`` names the directory being
written, so a mistake cannot touch a real config directory.

Fixtures carry fictional ids only; the public-safety scanner runs in CI.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

#: Any ISO-8601 naive-UTC instant, as the engine persists them.
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?$")

STATUS_KEYS = ("status", "last_success", "failures")


def require_sandbox() -> None:
    if not os.environ.get("XPST_CONFIG_DIR"):
        sys.exit("refusing to run: set XPST_CONFIG_DIR to a throwaway directory")


def manager_module():
    require_sandbox()
    from xpst.state_manager import StateManager

    return StateManager


def sandbox(target: Path) -> Path:
    """The target directory, asserted to be the one XPST_CONFIG_DIR names."""
    require_sandbox()
    resolved = target.expanduser().resolve()
    declared = Path(os.environ["XPST_CONFIG_DIR"]).expanduser().resolve()
    if resolved != declared:
        sys.exit(f"refusing to write {resolved}: XPST_CONFIG_DIR is {declared}")
    return resolved


def scenario(state: Any) -> None:
    """The canonical operation sequence, mirrored operation-for-operation in
    ``crates/xpst-state/tests/state_parity.rs``. Changing it here without
    changing it there is a parity failure, which is the intent."""
    StateManager = manager_module()
    state.add_posted_video(
        video_id="vid-a",
        source_url="https://example.invalid/src/a",
        source_platform="youtube",
        posted_to={
            "youtube": {"id": "yt-a", "url": "https://example.invalid/v/yt-a"},
            "x": {"id": "x-a", "url": "https://example.invalid/v/x-a"},
        },
        caption="first caption",
        content_hash="hash-a",
    )
    state.add_posted_video(
        video_id="vid-b",
        source_url="https://example.invalid/src/b",
        source_platform="instagram",
        posted_to={
            "tiktok": {"id": "tt-b", "url": "https://example.invalid/v/tt-b"},
            "instagram": {"id": "ig-b", "url": "https://example.invalid/v/ig-b"},
        },
        caption="",
        content_hash="hash-b",
    )
    state.add_posted_video(
        video_id="vid-c",
        source_url="https://example.invalid/src/c",
        source_platform="local",
        posted_to={
            "x": {"id": "x-c", "url": "https://example.invalid/v/x-c"},
        },
        caption="",
        content_hash="hash-c",
    )
    # One platform goes, the other stays: the hash must survive.
    state.remove_post("vid-a", "x")
    # The last platform goes: the video and its hash must disappear.
    state.remove_post("vid-c", "x")
    state.record_failure("vid-b", "youtube", "upload rejected: expired token")
    state.record_failure("vid-b", "youtube", "upload rejected: expired token")
    state.add_posted_video(
        video_id="vid-b",
        source_url="",
        source_platform="",
        posted_to={
            "youtube": {"id": "yt-b", "url": "https://example.invalid/v/yt-b"},
        },
        caption="",
        content_hash="hash-b",
    )
    state.record_delete_tombstone(
        "vid-b", "tiktok", reason="hard_delete", detail="removed in studio"
    )
    state.set_visibility("vid-b", "instagram", "private")
    state.mark_delete_pending(
        "vid-a", "youtube", reason="confirmation_outstanding", detail="API returned 202"
    )


def canonical(document: Any) -> str:
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: normalize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize(item) for item in value]
    if isinstance(value, str) and TIMESTAMP_RE.match(value):
        return "<ts>"
    return value


def dump(path: Path, normalized: bool = False) -> str:
    document = json.loads(path.read_text(encoding="utf-8"))
    return canonical(normalize(document) if normalized else document)


def verify(dir_path: Path) -> int:
    """Assert the semantics of a document Rust wrote, through Python's API."""
    StateManager = manager_module()
    state = StateManager(dir_path)
    problems: list[str] = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            problems.append(message)

    raw = state.state
    check(raw.get("version") == 2, f"version is {raw.get('version')!r}, expected 2")
    check(
        sorted(raw.get("posted_videos", {})) == ["vid-a", "vid-b"],
        f"tracked videos are {sorted(raw.get('posted_videos', {}))}, expected vid-a and vid-b",
    )

    # The post id contract: `id`, never `post_id`.
    for video_id, video in raw.get("posted_videos", {}).items():
        for platform, entry in (video.get("posted_to") or {}).items():
            check(
                "post_id" not in entry,
                f"{video_id}/{platform} carries the legacy post_id key",
            )
            check(
                bool(entry.get("id")),
                f"{video_id}/{platform} has no id under the canonical key",
            )

    check(state.is_posted("vid-a", "youtube"), "vid-a should still be posted on youtube")
    check(not state.is_posted("vid-a", "x"), "vid-a/x was removed and must not count")
    check(
        not state.is_posted("vid-b", "tiktok"),
        "vid-b/tiktok is a tombstone and must not count as posted",
    )
    check(
        state.is_posted("vid-b", "instagram"),
        "vid-b/instagram is soft-hidden, not deleted, and must still count as posted",
    )
    check(
        state.is_fully_cross_posted("vid-b", ["instagram", "youtube"]),
        "vid-b is on instagram and youtube",
    )
    check(
        not state.is_fully_cross_posted("vid-b", ["instagram", "tiktok"]),
        "vid-b/tiktok is tombstoned, so it is not fully cross-posted",
    )

    check(state.has_hash("hash-a"), "hash-a must survive while vid-a is on youtube")
    check(not state.has_hash("hash-c"), "hash-c must be gone with its last platform")
    check(state.get_by_hash("hash-b") == "vid-b", "hash-b maps to vid-b")
    check(
        state.find_video_id_by_platform_post("youtube", "yt-b") == "vid-b",
        "the platform post id resolves back to vid-b",
    )

    pending = raw["posted_videos"]["vid-a"]["posted_to"]["youtube"]
    check(pending.get("delete_pending") is True, "vid-a/youtube should be delete-pending")
    check(
        pending.get("deleted") is False,
        "a pending delete is not a delete: deleted must be false",
    )
    check(
        pending.get("delete_pending_reason") == "confirmation_outstanding",
        "the pending reason is recorded",
    )
    check(
        pending.get("url") == "https://example.invalid/v/yt-a",
        "the pending entry keeps its url so a human can act on it",
    )

    tombstone = raw["posted_videos"]["vid-b"]["posted_to"]["tiktok"]
    check(tombstone.get("deleted") is True, "vid-b/tiktok should be a tombstone")
    check(
        tombstone.get("deleted_reason") == "hard_delete",
        "the tombstone reason is recorded",
    )
    check(
        tombstone.get("deleted_detail") == "removed in studio",
        "the tombstone detail is recorded",
    )
    check(
        "delete_pending" not in tombstone,
        "a hard delete clears the pending markers",
    )

    soft = raw["posted_videos"]["vid-b"]["posted_to"]["instagram"]
    check(soft.get("visibility") == "private", "the soft hide records its visibility")
    check(soft.get("soft_hidden") is True, "the soft hide is marked")
    check("deleted" not in soft, "a soft hide is not a delete")

    errors = raw["posted_videos"]["vid-b"].get("errors", {})
    check(errors.get("youtube", {}).get("count") == 2, "the failure count is kept")
    check(
        errors.get("youtube", {}).get("error") == "upload rejected: expired token",
        "the failure message is kept",
    )
    dlq = state.get_dead_letter_queue()
    check(
        len(dlq) == 1 and dlq[0]["status"] == "resolved",
        f"the dead-letter entry should be resolved, got {dlq}",
    )

    stats = state.get_statistics()
    check(stats["total_videos_tracked"] == 2, "two videos are tracked")
    check(stats["total_processed"] == 4, "four successful publishes were recorded")

    # Timestamps: real naive-UTC ISO strings, not placeholders, and recent.
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    for label, value in timestamps(raw):
        if not TIMESTAMP_RE.match(value):
            problems.append(f"{label} is not a naive ISO-8601 UTC timestamp: {value!r}")
            continue
        parsed = dt.datetime.fromisoformat(value)
        drift = abs((now - parsed).total_seconds())
        if drift > 600:
            problems.append(f"{label} is {drift:.0f}s away from now: {value}")

    for problem in problems:
        print(f"FAIL {problem}", file=sys.stderr)
    if problems:
        return 1
    print("python verified the rust-written state document")
    return 0


def timestamps(document: Any, prefix: str = "") -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(document, dict):
        for key, value in document.items():
            found.extend(timestamps(value, f"{prefix}.{key}" if prefix else key))
    elif isinstance(document, list):
        for index, value in enumerate(document):
            found.extend(timestamps(value, f"{prefix}[{index}]"))
    elif isinstance(document, str) and TIMESTAMP_RE.match(document):
        found.append((prefix, document))
    return found


def hold_lock(dir_path: Path, seconds: float) -> int:
    """Take the engine's cross-process lock and hold it.

    Used by the Rust tests to prove the two implementations exclude each other:
    the Rust writer must block until this process releases the lock. The marker
    file is written only once the lock is held, so the test never races the
    acquisition.
    """
    require_sandbox()
    lock_path = dir_path / ".state.lock"
    lock_path.touch(exist_ok=True)
    with open(lock_path, "a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        (dir_path / "lock-held").write_text("held", encoding="utf-8")
        time.sleep(seconds)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "mode", choices=["create", "dump", "dump-normalized", "verify", "hold-lock"]
    )
    parser.add_argument("target", type=Path)
    parser.add_argument("seconds", type=float, nargs="?")
    args = parser.parse_args()

    if args.mode == "hold-lock":
        return hold_lock(sandbox(args.target), args.seconds or 1.0)

    if args.mode == "create":
        StateManager = manager_module()
        directory = sandbox(args.target)
        directory.mkdir(parents=True, exist_ok=True)
        scenario(StateManager(directory))
        print(f"python created {directory / 'state.json'}")
        return 0
    if args.mode == "verify":
        return verify(sandbox(args.target))
    if args.mode == "dump-normalized":
        print(dump(args.target, normalized=True), end="")
        return 0
    print(dump(args.target), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
