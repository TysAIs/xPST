"""`xpst kb ...` command group. Heavy imports happen inside commands so the
core CLI can attach this group without loading faster-whisper."""
from __future__ import annotations

import click
from rich.console import Console

console = Console()


def _build_transcriber():
    """Isolated so tests can monkeypatch it. Raises a friendly error if the
    knowledge extra is not installed."""
    try:
        from xpst.knowledge.ingest.transcribe import FasterWhisperTranscriber
    except ImportError as exc:  # pragma: no cover - exercised manually
        raise click.ClickException(
            "Knowledge features need the extra: pip install 'xpst[knowledge]'"
        ) from exc
    return FasterWhisperTranscriber()


@click.group()
def kb() -> None:
    """Knowledge base: ingest videos and query extracted knowledge."""


@kb.command("add")
@click.argument("source")
@click.option("--workspace", "-w", default="default", help="Workspace name")
def kb_add(source: str, workspace: str) -> None:
    """Ingest a local file or URL into the knowledge base."""
    from xpst.knowledge.ingest.pipeline import ingest
    from xpst.knowledge.store.json_store import JsonKnowledgeStore
    from xpst.knowledge.workspace import Workspace

    ws = Workspace.resolve(workspace)
    store = JsonKnowledgeStore(ws.nuggets_path)
    nugget = ingest(source, store=store, transcriber=_build_transcriber())
    console.print(f"[green]Ingested[/green] {source} -> nugget {nugget.id}")


@kb.command("query")
@click.argument("text")
@click.option("--workspace", "-w", default="default", help="Workspace name")
def kb_query(text: str, workspace: str) -> None:
    """Return stored nuggets whose text matches (Phase 1: substring match)."""
    from xpst.knowledge.store.json_store import JsonKnowledgeStore
    from xpst.knowledge.workspace import Workspace

    ws = Workspace.resolve(workspace)
    store = JsonKnowledgeStore(ws.nuggets_path)
    needle = text.lower()
    hits = [n for n in store.all_nuggets() if needle in n.point.lower()]
    if not hits:
        console.print("[yellow]No matching nuggets.[/yellow]")
        return
    for n in hits:
        cite = n.source_url or n.source_video_id
        console.print(
            f"[bold]{n.point}[/bold]\n  ({cite} @ "
            f"{n.timestamp_start:.1f}-{n.timestamp_end:.1f}s)"
        )
