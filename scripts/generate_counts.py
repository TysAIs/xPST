#!/usr/bin/env python3
"""Generate (and verify) the surface counts published in README.md.

README.md advertised a hand-maintained set of headline numbers that drifted from
the product repeatedly (28 tools / 38 commands / "23 Tools" while the code served
38 / 44 / 38). The numbers a stranger reads first are now derived from the code:

    python scripts/generate_counts.py --write    # rewrite the README block
    python scripts/generate_counts.py --check    # non-zero exit on drift
    python scripts/generate_counts.py --json     # machine-readable counts

``--check`` does two things:

1. the explicitly marked README block must equal the freshly rendered block, and
2. every *claim* pattern in README (badges included) must match the live count,
   so a number cannot be reintroduced by hand somewhere outside the block.

Counts are measured from the shipped surfaces, never asserted:

* **MCP tools** — a real stdio handshake (``initialize`` + ``tools/list``) against
  ``python -m xpst mcp start``; falls back to the registry import when the ``mcp``
  extra is not installed (``--no-handshake`` forces the registry path).
* **CLI commands** — the Click tree: top-level commands and leaf commands
  (subcommands included), from ``xpst.cli.main``.
* **HTTP routes** — the dashboard FastAPI app's route table.
* **Providers** — ``provider_truth.SUPPORTED_PROVIDERS``.

The probe is read-only: ``XPST_MCP_READONLY=1`` is exported for the handshake and
no network call is made beyond the local stdio pipe.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
README_PATH = REPO_ROOT / "README.md"
BEGIN = "<!-- BEGIN GENERATED SURFACE COUNTS -->"
END = "<!-- END GENERATED SURFACE COUNTS -->"

# Framework-owned routes FastAPI always adds (docs/OpenAPI), plus the dashboard's
# own API/page routes. Both are reported so the number cannot be quoted vaguely.
_FRAMEWORK_ROUTE_PATHS = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


@dataclass(frozen=True)
class SurfaceCounts:
    """Live counts for every number this repository publishes about its surfaces."""

    mcp_tools: int
    mcp_source: str
    cli_top_level: int
    cli_leaf: int
    http_routes: int
    http_routes_app: int
    providers: int


# --------------------------------------------------------------------------- #
# measurement
# --------------------------------------------------------------------------- #
def cli_command_counts() -> tuple[int, int]:
    """(top-level commands, leaf commands including subcommands)."""
    from xpst.cli import main as cli_main

    def leaves(group) -> int:
        total = 0
        for command in group.commands.values():
            subcommands = getattr(command, "commands", None)
            total += leaves(command) if subcommands else 1
        return total

    return len(cli_main.commands), leaves(cli_main)


def mcp_tool_count(prefer_handshake: bool = True) -> tuple[int, str]:
    """(tool count, where it came from). Prefers a real stdio handshake."""
    if prefer_handshake:
        try:
            return _mcp_tool_count_over_stdio(), "stdio handshake"
        except ModuleNotFoundError:
            pass  # `mcp` extra absent — fall back to the registry below
        except Exception as exc:  # noqa: BLE001 - report, then degrade to registry
            print(f"warning: stdio handshake failed ({exc}); using the registry", file=sys.stderr)

    from xpst.mcp import server as mcp_server

    return len(mcp_server.TOOLS), "registry import"


def _mcp_tool_count_over_stdio(timeout_s: int = 120) -> int:
    """Ask a spawned ``xpst mcp start`` for its ``tools/list`` over stdio."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = dict(os.environ)
    src = str(REPO_ROOT / "src")
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    env["XPST_MCP_READONLY"] = "1"  # the probe never mutates

    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "xpst", "mcp", "start"],
        env=env,
    )

    async def _list() -> int:
        # The child's INFO logging (credential store, source init) would otherwise
        # interleave with --json output and test capture.
        with open(os.devnull, "w", encoding="utf-8") as devnull:
            async with stdio_client(server, errlog=devnull) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    return len(listed.tools)

    return asyncio.run(asyncio.wait_for(_list(), timeout=timeout_s))


def http_route_counts() -> tuple[int, int]:
    """(route-table entries, entries owned by xPST rather than FastAPI).

    The app is built with auth warm-up disabled so the count never depends on
    live credentials or network state (the same switch the test suite uses).
    """
    from xpst.dashboard.server import _create_app

    previous = os.environ.get("XPST_DISABLE_AUTH_WARM")
    os.environ["XPST_DISABLE_AUTH_WARM"] = "1"
    try:
        with tempfile.TemporaryDirectory(prefix="xpst-counts-") as config_dir:
            app = _create_app(config_dir=config_dir)
            paths = [getattr(route, "path", None) for route in app.routes]
            total = len(paths)
            app_owned = sum(1 for path in paths if path not in _FRAMEWORK_ROUTE_PATHS)
    finally:
        if previous is None:
            os.environ.pop("XPST_DISABLE_AUTH_WARM", None)
        else:
            os.environ["XPST_DISABLE_AUTH_WARM"] = previous
    return total, app_owned


def provider_count() -> int:
    from xpst.provider_truth import SUPPORTED_PROVIDERS

    return len(SUPPORTED_PROVIDERS)


def measure(prefer_handshake: bool = True) -> SurfaceCounts:
    top_level, leaf = cli_command_counts()
    tools, source = mcp_tool_count(prefer_handshake=prefer_handshake)
    routes, app_routes = http_route_counts()
    return SurfaceCounts(
        mcp_tools=tools,
        mcp_source=source,
        cli_top_level=top_level,
        cli_leaf=leaf,
        http_routes=routes,
        http_routes_app=app_routes,
        providers=provider_count(),
    )


# --------------------------------------------------------------------------- #
# README block
# --------------------------------------------------------------------------- #
def render_block(counts: SurfaceCounts) -> str:
    """The generated README region: the numbers and how each one is measured."""
    return "\n".join(
        [
            "",
            "| Surface | Count | Measured from |",
            "|---------|-------|---------------|",
            f"| MCP tools | **{counts.mcp_tools}** | `tools/list` over a real stdio "
            "handshake with `xpst mcp start` |",
            f"| CLI top-level commands | **{counts.cli_top_level}** | `xpst.cli.main.commands` |",
            f"| CLI commands including subcommands | **{counts.cli_leaf}** | recursive walk of the "
            "Click command tree |",
            f"| HTTP routes (dashboard app) | **{counts.http_routes}** | FastAPI route table "
            f"({counts.http_routes_app} xPST routes + "
            f"{counts.http_routes - counts.http_routes_app} framework docs routes) |",
            f"| Supported providers | **{counts.providers}** | "
            "`xpst.provider_truth.SUPPORTED_PROVIDERS` |",
            "",
            "Regenerate and verify with `python scripts/generate_counts.py --write` / `--check`; "
            "the check runs in CI, so these numbers cannot drift silently.",
            "",
        ]
    )


def apply_block(text: str, block: str) -> str:
    start = text.index(BEGIN) + len(BEGIN)
    end = text.index(END)
    return text[:start] + block + text[end:]


def _strip_generated_blocks(text: str) -> str:
    """Claim scanning ignores generated regions (they are checked by construction)."""
    pattern = re.compile(re.escape(BEGIN) + ".*?" + re.escape(END), re.DOTALL)
    return pattern.sub("", text)


# (regex with one integer group, attribute name on SurfaceCounts, label, required?)
# ``required`` rules must keep matching somewhere in README — removing a headline
# claim silently is how numbers went missing in the first place. Optional rules
# cover phrasings that may live only in the generated block.
CLAIM_RULES: tuple[tuple[str, str, str, bool], ...] = (
    (r"MCP-(\d+)%20tools", "mcp_tools", "MCP badge", True),
    (r"\*\*MCP server\*\* — (\d+) tools", "mcp_tools", "MCP server bullet", True),
    (r"(?<![\w.])(\d+) top-level commands", "cli_top_level", "top-level command claims", True),
    (r"(?<![\w.])(\d+) Click-based commands", "cli_top_level", "CLI bullet", True),
    (
        r"(?<![\w.])(\d+) commands including subcommands",
        "cli_leaf",
        "leaf command claims",
        False,
    ),
    (r"(?<![\w.])(\d+) HTTP routes", "http_routes", "HTTP route claims", False),
    (r"badge/platforms-(\d+)", "providers", "platforms badge", True),
)

# Numbers that must not be hand-asserted at all (they change per runner/commit).
FORBIDDEN_CLAIMS: tuple[tuple[str, str], ...] = (
    (r"badge/tests-(\d+)", "the tests badge"),
    (r"(\d+) passed / (\d+) skipped", "a hard-coded test pass/skip count"),
    (r"(\d+)\s+passed,\s*(\d+)\s+skipped", "a hard-coded test pass/skip count"),
)

# The desktop page table's heading number must equal its own row count.
_PAGES_HEADING = re.compile(r"###\s+(\d+)\s+Pages")
_PAGES_ROW = re.compile(r"^\|\s*\*\*[^*]+\*\*\s*\|")


def check_claims(counts: SurfaceCounts, text: str) -> list[str]:
    """Every headline number claim in README, compared against the live counts."""
    scanned = _strip_generated_blocks(text)
    problems: list[str] = []

    for pattern, attribute, label, required in CLAIM_RULES:
        expected = getattr(counts, attribute)
        matches = list(re.finditer(pattern, scanned))
        if not matches:
            if required:
                problems.append(f"{label}: no claim matched /{pattern}/ (was the claim removed?)")
            continue
        for match in matches:
            claimed = int(match.group(1))
            if claimed != expected:
                line = scanned.count("\n", 0, match.start()) + 1
                problems.append(
                    f"{label} (README line ~{line}): claims {claimed}, live value is {expected}"
                )

    for pattern, label in FORBIDDEN_CLAIMS:
        match = re.search(pattern, scanned)
        if match:
            line = scanned.count("\n", 0, match.start()) + 1
            problems.append(
                f"{label} (README line ~{line}): {match.group(0)!r} is hand-asserted; "
                "point at CI instead"
            )

    heading = _PAGES_HEADING.search(scanned)
    if heading:
        section = scanned[heading.end() :]
        rows = 0
        for line in section.splitlines():
            if _PAGES_ROW.match(line):
                rows += 1
            elif rows and line.strip() and not line.startswith("|"):
                break
        if rows != int(heading.group(1)):
            problems.append(
                f"desktop page table: heading says {heading.group(1)} pages, table lists {rows}"
            )

    return problems


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _readme() -> str:
    return README_PATH.read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="rewrite the generated README block")
    mode.add_argument("--check", action="store_true", help="fail when README has drifted")
    mode.add_argument("--json", action="store_true", help="print the live counts as JSON")
    parser.add_argument(
        "--no-handshake",
        action="store_true",
        help="count MCP tools from the registry import instead of a stdio handshake",
    )
    args = parser.parse_args(argv)

    counts = measure(prefer_handshake=not args.no_handshake)

    if args.json:
        print(json.dumps(asdict(counts), indent=2, sort_keys=True))
        return 0

    text = _readme()
    if BEGIN not in text or END not in text:
        print(f"FAIL: generated count block markers missing from {README_PATH}", file=sys.stderr)
        return 2

    if args.write:
        README_PATH.write_text(apply_block(text, render_block(counts)), encoding="utf-8")
        print(f"wrote {README_PATH.relative_to(REPO_ROOT)} ({counts.mcp_tools} MCP tools, "
              f"{counts.cli_top_level} top-level / {counts.cli_leaf} leaf CLI commands, "
              f"{counts.http_routes} HTTP routes, {counts.providers} providers)")
        return 0

    problems = check_claims(counts, text)
    if apply_block(text, render_block(counts)) != text:
        problems.insert(
            0,
            "README generated count block is stale; run "
            "`python scripts/generate_counts.py --write`",
        )
    if problems:
        print("FAIL: README counts disagree with the live surfaces:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print(
        f"PASS: README matches the live surfaces — {counts.mcp_tools} MCP tools "
        f"({counts.mcp_source}), {counts.cli_top_level} top-level / {counts.cli_leaf} leaf CLI "
        f"commands, {counts.http_routes} HTTP routes, {counts.providers} providers"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
