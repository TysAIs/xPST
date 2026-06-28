"""List all broken local markdown links in docs/ for quick repair."""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]

LOCAL_MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\((?!https?://)([^)]+)\)")
LOCAL_MARKDOWN_LINK = re.compile(
    r"(?<!!)(?<!\\)\[[^\]\n]+\]\((?!https?://|mailto:|#)([^)\s]+(?:\s+\"[^\"]*\")?)\)"
)


def _local_markdown_targets(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    targets: list[str] = []

    for regex in (LOCAL_MARKDOWN_IMAGE, LOCAL_MARKDOWN_LINK):
        for match in regex.finditer(text):
            raw_target = match.group(1).strip()
            target = raw_target.split(" ", 1)[0].strip("<>")
            target = unquote(target).split("#", 1)[0]
            if target and not target.startswith(("/", "http:", "https:", "mailto:")):
                targets.append(target)

    return targets


SKIPPED_MARKDOWN_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "build",
    "dist",
}

broken_links: list[str] = []

for markdown_file in sorted(ROOT.rglob("*.md")):
    if any(part in SKIPPED_MARKDOWN_DIRS for part in markdown_file.relative_to(ROOT).parts):
        continue

    for target in _local_markdown_targets(markdown_file):
        if not (markdown_file.parent / target).exists():
            broken_links.append(f"{markdown_file.relative_to(ROOT)} -> {target}")

print(f"Broken links: {len(broken_links)}")
for item in broken_links:
    print(item)
if not broken_links:
    print("No broken links.")
