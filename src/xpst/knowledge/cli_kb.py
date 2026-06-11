"""`xpst kb ...` command group. Heavy imports happen inside commands so the
core CLI can attach this group without loading faster-whisper / fastembed /
lancedb."""
from __future__ import annotations

import click
from rich.console import Console

console = Console()


def _missing_extra(exc: Exception) -> click.ClickException:
    return click.ClickException(
        "Knowledge features need the extra: pip install 'xpst[knowledge]'"
    )


def _build_transcriber(config):
    """Isolated so tests can monkeypatch it. Raises a friendly error if the
    knowledge extra is not installed."""
    try:
        from xpst.knowledge.ingest.transcribe import FasterWhisperTranscriber
    except ImportError as exc:  # pragma: no cover - exercised manually
        raise _missing_extra(exc) from exc
    return FasterWhisperTranscriber(model_size=config.whisper_model)


def _build_embedder(config):
    """Isolated so tests can monkeypatch it."""
    from xpst.knowledge.llm.embeddings import build_embedder
    return build_embedder(config)


def _build_llm_client(config):
    """Isolated so tests can monkeypatch it."""
    from xpst.knowledge.llm.client import LLMClient
    return LLMClient(base_url=config.llm_base_url, model=config.llm_model,
                     api_key=config.llm_api_key)


@click.group()
def kb() -> None:
    """Knowledge base: ingest videos and query extracted knowledge."""


@kb.command("add")
@click.argument("source")
@click.option("--workspace", "-w", default="default", help="Workspace name")
def kb_add(source: str, workspace: str) -> None:
    """Ingest a local file or URL into the knowledge base."""
    from xpst.knowledge.config import KnowledgeConfig
    from xpst.knowledge.ingest.pipeline import ingest
    from xpst.knowledge.manifest import Manifest
    from xpst.knowledge.store.json_store import JsonKnowledgeStore
    from xpst.knowledge.workspace import Workspace

    config = KnowledgeConfig.from_env()
    ws = Workspace.resolve(workspace)
    store = JsonKnowledgeStore(ws.nuggets_path)
    manifest = Manifest(ws.manifest_path)
    result = ingest(
        source,
        store=store,
        transcriber=_build_transcriber(config),
        manifest=manifest,
        embedder=_build_embedder(config),
        llm_client=_build_llm_client(config),
    )
    if result.skipped:
        console.print(f"[yellow]Skipped[/yellow] {source} ({result.reason})")
    elif result.reason:
        console.print(f"[red]Failed[/red] {source}: {result.reason}")
        raise SystemExit(1)
    else:
        console.print(
            f"[green]Ingested[/green] {len(result.nuggets)} nuggets "
            f"from {source}"
        )


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


@kb.command("organize")
@click.option("--workspace", "-w", default="default", help="Workspace name")
@click.option("--threshold", "-t", default=None, type=float,
              help="Cosine similarity threshold for clustering/routing")
def kb_organize(workspace: str, threshold: float | None) -> None:
    """Discover areas, tag difficulty, and assign nuggets (Phase 3)."""
    from xpst.knowledge.config import KnowledgeConfig
    from xpst.knowledge.organize.cluster import DEFAULT_CLUSTER_THRESHOLD
    from xpst.knowledge.organize.pipeline import organize_store
    from xpst.knowledge.store.json_store import JsonKnowledgeStore
    from xpst.knowledge.workspace import Workspace

    config = KnowledgeConfig.from_env()
    ws = Workspace.resolve(workspace)
    store = JsonKnowledgeStore(ws.nuggets_path)
    thr = threshold if threshold is not None else DEFAULT_CLUSTER_THRESHOLD
    result = organize_store(store, _build_llm_client(config), threshold=thr)
    console.print(
        f"[green]Organized[/green] {result.nugget_count} nuggets into "
        f"{result.area_count} areas ({result.assigned} assigned)"
    )


@kb.command("areas")
@click.option("--workspace", "-w", default="default", help="Workspace name")
def kb_areas(workspace: str) -> None:
    """List discovered areas in course order (beginner -> advanced)."""
    from xpst.knowledge.organize.difficulty import order_areas
    from xpst.knowledge.store.json_store import JsonKnowledgeStore
    from xpst.knowledge.workspace import Workspace

    ws = Workspace.resolve(workspace)
    store = JsonKnowledgeStore(ws.nuggets_path)
    areas = order_areas(store.areas())
    if not areas:
        console.print("[yellow]No areas yet. Run 'xpst kb organize'.[/yellow]")
        return
    for area in areas:
        console.print(
            f"[bold]{area.order_index + 1}. {area.label}[/bold] "
            f"({len(area.nugget_ids)} nuggets)"
        )


@kb.command("course")
@click.option("--workspace", "-w", default="default", help="Workspace name")
@click.option("--area", "-a", "area_id", default=None,
              help="Assemble only this area (by id)")
def kb_course(workspace: str, area_id: str | None) -> None:
    """Emit the organized outline (areas -> ordered, cited nuggets) for an AI to
    write a course from. Pre-ordered beginner -> advanced."""
    from xpst.knowledge.course.assemble import assemble_course
    from xpst.knowledge.store.json_store import JsonKnowledgeStore
    from xpst.knowledge.workspace import Workspace

    ws = Workspace.resolve(workspace)
    store = JsonKnowledgeStore(ws.nuggets_path)
    course = assemble_course(store, workspace=ws.name, area_id=area_id)
    if not course.areas:
        console.print(
            "[yellow]Nothing to assemble. Ingest with 'xpst kb add' and "
            "organize with 'xpst kb organize'.[/yellow]"
        )
        return
    for area in course.areas:
        console.print(
            f"[bold]{area.order + 1}. {area.label}[/bold] "
            f"({len(area.nuggets)} nuggets)"
        )
        for nugget in area.nuggets:
            console.print(
                f"  - [{nugget.difficulty}] {nugget.point}\n"
                f"    ({nugget.citation} @ "
                f"{nugget.timestamp_start:.1f}-{nugget.timestamp_end:.1f}s)"
            )


@kb.command("doctor")
@click.option("--workspace", "-w", default="default", help="Workspace name")
def kb_doctor(workspace: str) -> None:
    """Health-check the knowledge base (read-only): deps, store integrity,
    queue state, embedding consistency, orphaned areas/nuggets."""
    from xpst.knowledge.doctor import (
        SEVERITY_ERROR,
        SEVERITY_OK,
        SEVERITY_WARNING,
        diagnose,
    )
    from xpst.knowledge.workspace import Workspace

    ws = Workspace.resolve(workspace)
    report = diagnose(ws)
    style = {
        SEVERITY_OK: "green",
        SEVERITY_WARNING: "yellow",
        SEVERITY_ERROR: "red",
    }
    for finding in report.findings:
        colour = style.get(finding.severity, "white")
        console.print(
            f"[{colour}]{finding.severity.upper():7}[/{colour}] "
            f"{finding.check}: {finding.message}"
        )
    verdict = "OK" if report.ok else "PROBLEMS FOUND"
    colour = "green" if report.ok else "red"
    console.print(
        f"[bold {colour}]{verdict}[/bold {colour}] "
        f"({report.error_count} errors, {report.warning_count} warnings)"
    )
    if not report.ok:
        raise SystemExit(1)
