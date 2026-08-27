"""
Polished first-run connection wizard for xPST.

One coherent experience that works identically well for humans in a terminal
and for AI agents over a pipe:

- Friendly intro explaining the one-time-per-platform human approval step.
- Per-platform, exact click-by-click instructions rendered in-terminal AND
  exportable as markdown (``xpst wizard --export-md FILE``).
- Credential entry/validation with an immediate health-check per platform,
  reusing :func:`xpst.connect.test_connections`.
- Durable progress state (``~/.xpst/wizard_state.json``) so re-running the
  wizard resumes where you left off.
- Non-TTY / ``--json`` agent mode: emits the checklist machine-readably and
  never prompts (the legacy wizard crashed with EOFError on pipes).
- End-to-end summary with pass/fail per platform plus the next action.

Usage:
    xpst wizard                    # full first-run experience (resumes)
    xpst wizard youtube            # single platform
    xpst wizard --json             # machine-readable checklist (agent mode)
    xpst wizard --export-md doc.md # reusable markdown guide
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from xpst.config import XPSTConfig
from xpst.utils.logger import get_logger

console = Console()
logger = get_logger(__name__)

WIZARD_STATE_FILENAME = "wizard_state.json"

INTRO_TEXT = """\
[bold blue]Welcome to xPST![/bold blue]

xPST watches a TikTok account and cross-posts videos to YouTube Shorts,
X/Twitter, Instagram Reels and more — automatically.

Connecting each platform is a [bold]one-time[/bold] step. Because these are
[yellow]your[/yellow] accounts, each platform requires [bold]you[/bold] (the
human) to approve access once — usually by logging in or clicking "Allow" in
your browser. xPST never sees or stores your password in plain text; tokens
are encrypted at rest.

This wizard will:
  1. Show you exactly what to click, step by step, for each platform.
  2. Collect credentials as needed (hidden as you type).
  3. Immediately verify each connection and show pass/fail.
  4. Remember your progress — if you stop halfway, just run it again.

You can re-run [cyan]xpst wizard[/cyan] at any time; it picks up where it
left off.
"""


@dataclass
class WizardStep:
    """One click-by-click instruction inside a platform's guide."""

    text: str


@dataclass
class PlatformGuide:
    """Everything the wizard needs to walk a human through one platform."""

    key: str
    title: str
    why: str
    steps: list[WizardStep] = field(default_factory=list)
    docs_url: str = ""


# ──────────────────────────────────────────────
# Per-platform click-by-click guides
# ──────────────────────────────────────────────

PLATFORM_GUIDES: dict[str, PlatformGuide] = {
    "youtube": PlatformGuide(
        key="youtube",
        title="YouTube Shorts",
        why="Publishes your videos as YouTube Shorts.",
        docs_url="https://github.com/TysAIs/xPST/blob/main/docs/youtube-oauth-production.md",
        steps=[
            WizardStep("Open https://console.cloud.google.com/apis/credentials"),
            WizardStep("Create (or select) a project."),
            WizardStep("Click 'Create Credentials' → 'OAuth 2.0 Client ID'."),
            WizardStep(
                "Application type: 'Desktop app'. Name it e.g. 'xPST Desktop'."
            ),
            WizardStep(
                "Download the JSON and save it as "
                "~/.xpst/credentials/youtube_client_secrets.json"
            ),
            WizardStep(
                "If your Google Cloud OAuth consent screen is still in "
                "'Testing' mode, either add your own account as a Test user, "
                "or click 'Publish App' to move it to production so tokens "
                "do not expire after 7 days."
            ),
            WizardStep(
                "Back in this wizard, press Enter — your browser opens for "
                "the one-time Google approval. Sign in and click 'Allow'."
            ),
        ],
    ),
    "instagram": PlatformGuide(
        key="instagram",
        title="Instagram Reels",
        why="Cross-posts your videos as Instagram Reels.",
        steps=[
            WizardStep(
                "Instagram posting uses the official Meta Graph API. You need "
                "a Meta developer app: open https://developers.facebook.com/apps"
            ),
            WizardStep("Click 'Create App' → type 'Business'."),
            WizardStep("Add the 'Instagram Graph API' product to the app."),
            WizardStep(
                "Connect your Instagram Business account to a Facebook Page "
                "(required by Meta). In Instagram: Settings → Business tools."
            ),
            WizardStep(
                "Generate a long-lived access token (Graph API Explorer → "
                "generate token with instagram_basic + instagram_content_publish "
                "permissions, then exchange for a long-lived token)."
            ),
            WizardStep(
                "Paste the token when the wizard asks. It also needs your "
                "numeric IG user ID (it can look this up automatically)."
            ),
        ],
    ),
    "x": PlatformGuide(
        key="x",
        title="X / Twitter",
        why="Posts your videos to X/Twitter.",
        steps=[
            WizardStep(
                "Option A (official API): open https://developer.x.com and "
                "create a Project + App. Free tier allows ~17 posts/day."
            ),
            WizardStep(
                "Generate the Bearer Token and paste it into the wizard — "
                "it verifies immediately against the X API."
            ),
            WizardStep(
                "Option B (cookies): log into x.com in your browser, export "
                "cookies (auth_token + ct0), and choose the cookies option "
                "in the wizard."
            ),
        ],
    ),
    "tiktok": PlatformGuide(
        key="tiktok",
        title="TikTok",
        why="Watches a TikTok account as the video source.",
        steps=[
            WizardStep(
                "For SOURCE mode (watching an account), you only need "
                "yt-dlp installed — no app required. The wizard checks this "
                "for you (`uv tool install yt-dlp` if missing)."
            ),
            WizardStep(
                "For UPLOADING to TikTok (Content Posting API): open "
                "https://developers.tiktok.com/apps and click 'Manage apps' → "
                "'Create app'."
            ),
            WizardStep(
                "Add the 'Content Posting API' product. Set a redirect URI "
                "(e.g. http://localhost:8080/callback). Note the client key "
                "and client secret."
            ),
            WizardStep(
                "Submit the app for audit; while pending you can still post "
                "to private/self-only audience from the same account."
            ),
        ],
    ),
    "threads": PlatformGuide(
        key="threads",
        title="Threads",
        why="Posts to Threads via the official Threads API.",
        steps=[
            WizardStep("Open https://developers.facebook.com/apps"),
            WizardStep("'Create App' → use case 'Threads API access'."),
            WizardStep("Connect your Threads profile and generate an access token."),
            WizardStep("Paste the token and your numeric Threads user ID into the wizard."),
        ],
    ),
    "messenger": PlatformGuide(
        key="messenger",
        title="Messenger (optional)",
        why="Optional: reply to viewers via Messenger webhook.",
        steps=[
            WizardStep("This is optional — press Enter to skip unless you need it."),
            WizardStep("In your Meta app, add the Messenger product."),
            WizardStep("Generate a Page Access Token and paste it into the wizard."),
        ],
    ),
}

PLATFORM_ORDER = ["youtube", "instagram", "x", "tiktok", "threads", "messenger"]


# ──────────────────────────────────────────────
# Progress state
# ──────────────────────────────────────────────

def _state_path(config: XPSTConfig) -> Path:
    return Path(config.config_dir).expanduser() / WIZARD_STATE_FILENAME


def load_wizard_state(config: XPSTConfig) -> dict:
    """Load persisted wizard progress (returns {} if absent/corrupt)."""

    path = _state_path(config)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        logger.debug("Ignoring corrupt wizard state at %s", path)
        return {}


def save_wizard_state(config: XPSTConfig, state: dict) -> None:
    """Persist wizard progress atomically."""

    path = _state_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


def _record_platform_result(state: dict, key: str, ok: bool, detail: str = "") -> None:
    entry = state.setdefault("platforms", {})
    entry[key] = {
        "status": "connected" if ok else "failed",
        "detail": detail,
        "updated_at": time.time(),
    }


def mark_onboarding_complete(config: XPSTConfig, state: dict | None = None) -> None:
    """Persist server-side that first-run onboarding finished.

    This is the single completion write path for every mode (interactive and
    agent/``--json``). It flips the persisted ``first_run_complete`` config
    flag (the authoritative gate read by the desktop app, ``run`` and
    ``status``) AND records a ``completed: true`` marker in
    ``wizard_state.json`` so the progress file can never be mistaken for an
    unfinished flow.

    Args:
        config: The xPST config to persist the flag on.
        state: Optional wizard progress dict to finalize. When omitted the
            current persisted progress is loaded (an absent file is fine —
            it degrades to ``{}``).
    """
    config.first_run_complete = True
    config.save()
    current = state if state is not None else load_wizard_state(config)
    current.setdefault("platforms", {})
    current["completed"] = True
    save_wizard_state(config, current)


def onboarding_required(config: XPSTConfig) -> bool:
    """Whether first-run onboarding should still be offered.

    The single source of truth is the persisted ``first_run_complete`` flag.
    Crucially, it never consults ``wizard_state.json``: a stale or missing
    progress file must never re-trigger onboarding for an install that has
    genuinely completed (defensive check — see audit 02-integrations.md §4).

    Args:
        config: The xPST config to inspect.

    Returns:
        True when onboarding is still required, False once ``first_run_complete``
        is True.
    """
    return not bool(getattr(config, "first_run_complete", False))


# ──────────────────────────────────────────────
# Rendering helpers
# ──────────────────────────────────────────────

def render_platform_markdown(guide: PlatformGuide) -> str:
    """Render one platform's guide as reusable markdown."""

    lines = [f"## {guide.title}", "", f"{guide.why}", ""]
    for i, step in enumerate(guide.steps, 1):
        lines.append(f"{i}. {step.text}")
    if guide.docs_url:
        lines += ["", f"More details: {guide.docs_url}"]
    return "\n".join(lines) + "\n"


def export_markdown(path: str | Path) -> Path:
    """Export the full per-platform guide as a markdown document."""

    parts = [
        "# xPST First-Run Connection Guide",
        "",
        "One-time, per-platform human approval steps. The in-app wizard "
        "(`xpst wizard`) shows the same instructions interactively.",
        "",
    ]
    for key in PLATFORM_ORDER:
        parts.append(render_platform_markdown(PLATFORM_GUIDES[key]))
        parts.append("")
    out = Path(path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(parts), encoding="utf-8")
    return out


def build_checklist(config: XPSTConfig) -> list[dict]:
    """Build the machine-readable checklist of all platforms.

    This is what agent mode consumes — pure data, no prompting.
    """

    results = {}
    try:
        import asyncio

        from xpst.connect import test_connections

        results = asyncio.run(test_connections(config))
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("test_connections failed: %s", e)

    state = load_wizard_state(config)
    checklist = []
    for key in PLATFORM_ORDER:
        guide = PLATFORM_GUIDES[key]
        health_ok = bool(results.get(key))
        prev = state.get("platforms", {}).get(key, {}).get("status")
        checklist.append(
            {
                "platform": key,
                "title": guide.title,
                "why": guide.why,
                "steps": [s.text for s in guide.steps],
                "docs_url": guide.docs_url,
                "health": "pass" if health_ok else "fail",
                "last_wizard_status": prev,
                "action": (
                    None
                    if health_ok
                    else f"xpst wizard {key}"
                ),
            }
        )
    return checklist


# ──────────────────────────────────────────────
# Non-TTY-safe prompting
# ──────────────────────────────────────────────

class WizardNonInteractiveError(Exception):
    """Raised when interactive input is required but stdin is not a TTY."""


def _interactive() -> bool:
    return sys.stdin.isatty()


def _safe_input(prompt: str) -> str:
    """Prompt for input, raising a clean error instead of EOFError on pipes."""

    if not _interactive():
        raise WizardNonInteractiveError(prompt)
    return console.input(prompt)


def _safe_confirm(message: str, default: bool = True) -> bool:
    if not _interactive():
        raise WizardNonInteractiveError(message)
    suffix = " [Y/n]: " if default else " [y/N]: "
    response = console.input(f"[cyan]{message}{suffix}[/cyan]").strip().lower()
    if not response:
        return default
    return response in ("y", "yes")


# ──────────────────────────────────────────────
# Main wizard entry points
# ──────────────────────────────────────────────

def run_wizard_json(
    platforms: list[str] | None = None,
    *,
    config: XPSTConfig | None = None,
) -> dict:
    """Agent/non-TTY mode: emit the checklist as data. Never prompts.

    Completing the flow counts as finishing onboarding: when every requested
    platform passes its health check (``all_pass``), the persisted
    ``first_run_complete`` flag is set and ``wizard_state.json`` is finalized,
    exactly like the interactive path. This lets bots/scripts drive the wizard
    over a pipe and have the completion recorded server-side.

    Args:
        platforms: Optional subset of platforms to scope the run to.
        config: Optional config to operate on (used by tests; defaults to the
            user config when omitted).

    Returns:
        The machine-readable checklist result dict.
    """

    cfg = config if config is not None else XPSTConfig.load()
    checklist = build_checklist(cfg)
    if platforms:
        wanted = set(platforms)
        checklist = [c for c in checklist if c["platform"] in wanted]
    passed = all(c["health"] == "pass" for c in checklist) if checklist else False
    if passed and checklist:
        state = load_wizard_state(cfg)
        for entry in checklist:
            _record_platform_result(
                state, entry["platform"], True, detail="verified via agent mode"
            )
        mark_onboarding_complete(cfg, state)
    return {
        "mode": "agent",
        "interactive": False,
        "checklist": checklist,
        "all_pass": passed,
        "completed": passed and bool(checklist),
        "next_action": (
            None if passed
            else next((c["action"] for c in checklist if c["action"]), None)
        ),
    }


def run_wizard(
    platforms: list[str] | None = None,
    json_mode: bool = False,
) -> bool:
    """Run the polished first-run wizard.

    Args:
        platforms: Subset of platforms to walk through (None = all).
        json_mode: Force agent mode even on a TTY.

    Returns:
        True when every requested platform passes its health check.
    """

    if json_mode or not sys.stdin.isatty():
        result = run_wizard_json(platforms)
        print(json.dumps(result, indent=2))
        return result["all_pass"]

    config = XPSTConfig.load()
    state = load_wizard_state(config)

    console.print(Panel(INTRO_TEXT, border_style="blue", title="xPST Setup"))
    if not _safe_confirm("Ready to connect your accounts?", default=True):
        console.print("[dim]No problem — run [cyan]xpst wizard[/cyan] any time.[/dim]")
        return False

    target = platforms or PLATFORM_ORDER
    results: dict[str, bool] = {}

    from xpst.connect import (
        connect_instagram,
        connect_messenger,
        connect_threads,
        connect_tiktok,
        connect_x,
        connect_youtube,
        test_connections,
    )

    connectors = {
        "youtube": connect_youtube,
        "instagram": connect_instagram,
        "x": connect_x,
        "tiktok": connect_tiktok,
        "threads": connect_threads,
        "messenger": connect_messenger,
    }

    import asyncio

    for key in target:
        guide = PLATFORM_GUIDES[key]
        prev = state.get("platforms", {}).get(key, {}).get("status")
        console.print(Panel(f"[bold]{guide.title}[/bold] — {guide.why}", style="cyan"))
        if prev == "connected":
            console.print("[green]✅ Previously connected — verifying…[/green]")
        for i, step in enumerate(guide.steps, 1):
            console.print(f"  [bold cyan]{i}.[/bold cyan] {step.text}")
        if guide.docs_url:
            console.print(f"  [dim]Docs: {guide.docs_url}[/dim]")
        console.print()

        if not _safe_confirm(f"Continue with {guide.title}?", default=True):
            results[key] = False
            continue

        ok = False
        detail = ""
        try:
            ok = bool(connectors[key](config))
        except WizardNonInteractiveError:
            raise
        except KeyboardInterrupt:
            console.print(f"\n[yellow]Skipped {guide.title}[/yellow]")
        except Exception as e:  # noqa: BLE001 - surface to user
            detail = str(e)[:200]
            logger.error("Connection failed for %s: %s", key, e)
            console.print(f"[red]❌ {guide.title} error: {detail}[/red]")

        # Immediate health check
        health_results = asyncio.run(test_connections(config))
        health_ok = bool(health_results.get(key))
        verdict = ok and health_ok
        icon = "✅" if verdict else "❌"
        console.print(f"  {icon} {guide.title}: "
                      f"{'connected & verified' if verdict else 'needs attention'}")

        _record_platform_result(state, key, verdict, detail)
        save_wizard_state(config, state)
        results[key] = verdict
        console.print()

    # End-to-end summary
    console.print(Panel("[bold]Summary[/bold]", style="blue"))
    failed = []
    for key in target:
        title = PLATFORM_GUIDES[key].title
        if results.get(key):
            console.print(f"  ✅ {title}")
        else:
            console.print(f"  ❌ {title} — next action: [cyan]xpst wizard {key}[/cyan]")
            failed.append(key)

    if not failed:
        # Every requested platform is connected — finalize first-run
        # onboarding server-side so later `run`/`status` invocations skip it.
        state["completed"] = True
        save_wizard_state(config, state)
        config.first_run_complete = True
    config.save()

    if not failed:
        console.print(
            "\n[bold green]🎉 All connected! Try:[/bold green] "
            "[cyan]xpst watch[/cyan]"
        )
        return True
    console.print(
        f"\n[yellow]{len(failed)} platform(s) still need attention.[/yellow] "
        "Re-run [cyan]xpst wizard[/cyan] — it resumes where you left off."
    )
    return False
