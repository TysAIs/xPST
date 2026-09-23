"""Guard: repo docs must not advertise TikTok as a ready publish destination.

TikTok destination publishing is blocked pending external TikTok developer
review; TikTok is a video *source* (downloader/cookie path). The public site
and repo description were corrected for this claim (t_a8682604, PR #195) and
the remaining repo-doc claims were purged (t_934ed1b4). This scan keeps the
repo from regressing: an agent reading AGENTS.md or docs/ must not conclude it
can publish to TikTok.

Rule set mirrors missions/work/xpst-site-check.py claim patterns, scoped to
repo markdown (AGENTS.md + docs/*.md). Historical records and files that
already frame TikTok honestly are excluded.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Excluded from the scan:
# - historical records (point-in-time statements about a past state)
# - files that already carry the honest framing (capability truth table,
#   source-only setup guide, availability-hedged MCP tutorial, README which
#   was already corrected to source-only)
EXCLUDED_BASENAMES = {
    "README.md",
    "INSTALL.md",
    "setup-tiktok.md",
    "TUTORIAL_MCP.md",
    "CHANGELOG.md",
    "ROADMAP.md",
    "QUALITY_REVIEW.md",
    "GAP_ANALYSIS.md",
}

# P1: "post/upload ... to TikTok" with no disclaimer fragment later in the
#     sentence ("not available yet", "awaits review", ...).
# P2: "engagement from ... TikTok" analytics claims not clarified as
#     source-side (TikTok analytics use the downloader metadata path).
# P3: the removed "six destinations" phrasing.
# P4: comma-separated platform lists naming TikTok directly after a
#     post/upload verb (covers AGENTS.md's one-line pitch and the
#     ARCHITECTURE.md Platforms bullet).
AFFIRMATIVE_TIKTOK_PUBLISH_PATTERNS = [
    re.compile(
        r"(?:\bpost|\bupload)\w*[^.\n]{0,80}?\bto\b[^.\n]{0,80}?\btiktok\b"
        r"(?![^.\n]{0,160}\b(not available|blocked|awaits|pending|review|source[- ]side|not yet)\b)",
        re.I,
    ),
    re.compile(
        r"engag\w+\s+from\s+[^.\n]{0,100}?\btiktok\b"
        r"(?![^.\n]{0,160}\b(source[- ]side|not available)\b)",
        re.I,
    ),
    re.compile(r"six destinations", re.I),
    re.compile(
        r"(?:\bpost\w*|\bupload\w*)[^.\n]{0,60}?"
        r"(?:YouTube,\s*Instagram,\s*X/Twitter,\s*TikTok|X,\s*(?:>\s*)?Instagram,\s*TikTok,)",
        re.I,
    ),
]


def _iter_repo_docs() -> list[Path]:
    paths = [REPO_ROOT / "AGENTS.md"]
    paths.extend(sorted((REPO_ROOT / "docs").glob("*.md")))
    return paths


def _hits(text: str) -> list[str]:
    found: list[str] = []
    for pattern in AFFIRMATIVE_TIKTOK_PUBLISH_PATTERNS:
        for match in pattern.finditer(text):
            line_no = text[: match.start()].count("\n") + 1
            line = text.splitlines()[line_no - 1].strip()
            found.append(f"line {line_no}: {line[:200]}")
    return found


def test_no_affirmative_tiktok_publish_claims_in_repo_docs() -> None:
    """Every non-historical doc must frame TikTok as source-only."""
    offenders: dict[str, list[str]] = {}
    for path in _iter_repo_docs():
        if path.name in EXCLUDED_BASENAMES or not path.exists():
            continue
        found = _hits(path.read_text(encoding="utf-8"))
        if found:
            offenders[str(path.relative_to(REPO_ROOT))] = found
    assert not offenders, (
        "Affirmative TikTok-publishing claims found in repo docs:"
        + "".join(f"\n  {path}: {'; '.join(lines)}" for path, lines in offenders.items())
        + "\nFrame TikTok as a source; destination publishing is pending external "
        "review — mirror docs/INSTALL.md's capability truth table."
    )


def test_guard_catches_the_claim_forms_this_task_removed() -> None:
    """The rule set must fire on the exact pre-fix wording (no vacuous scan)."""
    removed_claims = [
        # AGENTS.md line 3 before the fix
        "**Enterprise-grade Python CLI/desktop app for automated video "
        "cross-posting to YouTube, X, Instagram, TikTok.**",
        # docs/MCP_TOOLS.md line 7 before the fix
        "xPST posts to six destinations — YouTube, Instagram, X/Twitter, TikTok, "
        "Threads, and Messenger (messaging/auto-reply) — and pulls source video "
        "from TikTok, YouTube, Instagram, X, and local files.",
        # docs/COMPETITIVE_EDGE.md README positioning line before the fix
        '> "The open-source, self-hosted, agent-native content engine. Post to '
        "YouTube, X,\n> Instagram, TikTok, and Threads with official OAuth; let "
        'AI agents drive it over MCP."',
        # docs/ARCHITECTURE.md Platforms bullet before the fix
        "- **Platforms**: Upload videos (YouTube, Instagram, X/Twitter, TikTok, Threads)",
        # docs/DASHBOARD.md analytics wording before the fix
        "collects per-post engagement from YouTube, Instagram, X, and TikTok APIs "
        "and caches snapshots",
    ]
    for text in removed_claims:
        assert _hits(text), f"rule set failed to catch removed claim: {text[:80]}..."


def test_disclaimer_framed_tiktok_wording_is_not_flagged() -> None:
    """Honest source-only framing must pass the scan."""
    allowed_wording = [
        "TikTok destination publishing is not available yet and awaits external "
        "TikTok developer review.",
        "Use it to source content for other destinations while publishing awaits "
        "external review.",
        "collects per-post engagement from YouTube, Instagram, X, and TikTok "
        "(TikTok via the source-side downloader metadata path)",
        "Download videos from TikTok and cross-post them to YouTube.",
    ]
    for text in allowed_wording:
        assert not _hits(text), f"honest wording was flagged: {text[:80]}..."
