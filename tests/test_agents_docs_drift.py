"""AGENTS.md must not drift from the code it describes.

``AGENTS.md`` is the first file an agent reads, so a wrong map is worse than no map:
the previous revision described the deleted PySide6/QML desktop app, quoted a CLI
command total that no longer existed, and quoted a test count that had drifted by
~1,000 tests. Agents followed it into files that were about to be removed.

These tests are deliberately generic — they re-derive every count from the live
registry instead of comparing against a hard-coded number, so they keep working
when the counts change (and fail when ``AGENTS.md`` is not updated with them).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENTS_MD = REPO_ROOT / "AGENTS.md"

# The superseded native UI stack. It must not be described anywhere in AGENTS.md:
# anything an agent reads there should point at code that is meant to survive.
REMOVED_DESKTOP_APP_MARKERS: tuple[str, ...] = (
    "pyside6",
    "pyside",
    "desktop_app",
    "qml",
    "xpst app",
    "xpst desktop",
    "python -m xpst app",
    "build.sh",
)

# Substrings that only ever appear in a stale claim. Checked case-insensitively.
KNOWN_STALE_STRINGS: tuple[str, ...] = (
    "1555",
    "1,555",
    "40 commands",
    "38 cmds",
    "10 qml pages",
)

# ``38 tools``, ``38 MCP tools``, ``40 commands``, ``1555 passed``, ...
_COUNT_CLAIM_RE = re.compile(r"(\d[\d,]*)\s+(mcp\s+)?(tools|commands|passed|skipped)\b", re.IGNORECASE)
# ``python -m xpst <verb>`` anywhere in the file.
_PY_MODULE_CMD_RE = re.compile(r"python\s+-m\s+xpst\s+([a-z][a-z0-9-]*)")
# Backticked bare ``xpst <verb>`` (not ``xpst_delete`` / ``xpst://`` / ``xpst[extra]``).
_BARE_CMD_RE = re.compile(r"`xpst\s+([a-z][a-z0-9-]*)")


def _agents_text() -> str:
    return AGENTS_MD.read_text(encoding="utf-8")


def _live_mcp_tool_count() -> int:
    from xpst.mcp import server as mcp_server

    return len(mcp_server.TOOLS)


def _live_cli_commands() -> set[str]:
    from xpst.cli import main

    return set(main.commands)


def _live_cli_leaf_count() -> int:
    from xpst.cli import main

    def walk(group) -> list[str]:
        found: list[str] = []
        for name, cmd in group.commands.items():
            sub = getattr(cmd, "commands", None)
            found.extend(walk(cmd) if sub else [name])
        return found

    return len(walk(main))


def test_agents_md_describes_no_removed_desktop_app() -> None:
    text = _agents_text().lower()
    hits = sorted({marker for marker in REMOVED_DESKTOP_APP_MARKERS if marker in text})
    assert hits == [], (
        "AGENTS.md points agents at the removed native desktop app "
        f"({hits}). It must describe the Python engine plus the Rust/Tauri shell."
    )


def test_agents_md_quotes_no_known_stale_string() -> None:
    text = _agents_text().lower()
    hits = [stale for stale in KNOWN_STALE_STRINGS if stale in text]
    assert hits == [], f"AGENTS.md contains previously-corrected stale claims: {hits}"


def test_agents_md_names_mcp_as_the_primary_agent_surface() -> None:
    text = _agents_text().lower()
    assert "primary agent surface" in text
    assert "scriptable fallback" in text
    # The MCP table row is what tests/test_mcp_docs_registry_parity.py keys on; if the
    # row stops matching, that check silently stops covering AGENTS.md.
    assert re.search(r"\|\s*\*\*MCP\*\*\s*\|[^|]*\|\s*\d+ tools \(", _agents_text()), (
        "AGENTS.md lost its `| **MCP** | ... | <N> tools (...)` row"
    )


def test_agents_md_count_claims_match_the_live_registries() -> None:
    text = _agents_text()
    tool_total = _live_mcp_tool_count()
    cli_totals = {len(_live_cli_commands()), _live_cli_leaf_count()}

    problems: list[str] = []
    for raw, _mcp_prefix, noun in _COUNT_CLAIM_RE.findall(text):
        claimed = int(raw.replace(",", ""))
        noun = noun.lower()
        if noun == "tools" and claimed != tool_total:
            problems.append(f"claims {claimed} MCP tools, registry serves {tool_total}")
        elif noun == "commands" and claimed not in cli_totals:
            problems.append(f"claims {claimed} CLI commands, live CLI has {sorted(cli_totals)}")
        elif noun in {"passed", "skipped"}:
            problems.append(f"quotes a test count ({raw} {noun}); test counts drift and are not drift-checked")
    assert not problems, "stale counts in AGENTS.md:\n" + "\n".join(problems)


def test_every_command_agents_md_names_exists() -> None:
    text = _agents_text()
    named = set(_PY_MODULE_CMD_RE.findall(text)) | set(_BARE_CMD_RE.findall(text))
    assert named, "AGENTS.md no longer tells an agent how to run anything"
    missing = sorted(named - _live_cli_commands())
    assert missing == [], f"AGENTS.md tells agents to run commands that do not exist: {missing}"


def test_agents_md_states_the_pr_only_and_isolated_checkout_rules() -> None:
    text = _agents_text()
    assert re.search(r"PRs only", text), "missing the PR-only publishing rule"
    assert "refs/heads/" in text, "missing the explicit-ref push form"
    assert "throwaway clone" in text, "missing the isolated-checkout rule"
    assert "PYTHONPATH" in text and "$PWD/src" in text, (
        "missing the PYTHONPATH trap from the checkout rule (a clone can silently test the shared tree without it)"
    )
