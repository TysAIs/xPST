"""Read-only health check for a knowledge-base workspace (``kb doctor``).

Diagnoses, never mutates. It reports on:

* optional-extra dependency presence (faster-whisper / fastembed / lancedb),
  probed without importing them so the cold-path wall stays intact;
* store integrity (nuggets/areas load, area membership points at real nuggets);
* queue state (counts per status, surfacing failed items);
* embedding / vector consistency (a single embedding width across the store --
  a mix means the embedding model changed without a re-embed, which corrupts
  centroids, see ``organize/_vectors.centroid``);
* orphaned areas (empty membership) and orphaned nuggets (assigned to an
  ``area_id`` that no longer exists).

Each finding has a severity. ``ok`` is True only when no ``error`` finding
exists; ``warning`` findings (e.g. optional deps absent, store empty) do not
fail the check. The result is a plain dataclass so both the CLI and tests can
read it without parsing rendered text.
"""
from __future__ import annotations

import importlib.util
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

from xpst.knowledge.manifest import Manifest
from xpst.knowledge.queue import IngestionQueue
from xpst.knowledge.store.json_store import JsonKnowledgeStore

if TYPE_CHECKING:  # pragma: no cover - typing only
    from xpst.knowledge.workspace import Workspace

SEVERITY_OK = "ok"
SEVERITY_WARNING = "warning"
SEVERITY_ERROR = "error"

# Heavy optional-extra deps probed (not imported) so the wall is never breached.
_OPTIONAL_DEPS = ("faster_whisper", "fastembed", "lancedb")


@dataclass(frozen=True)
class Finding:
    check: str
    severity: str
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class DoctorReport:
    workspace: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not any(f.severity == SEVERITY_ERROR for f in self.findings)

    @property
    def error_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == SEVERITY_ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == SEVERITY_WARNING)

    def to_dict(self) -> dict:
        return {
            "workspace": self.workspace,
            "ok": self.ok,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "findings": [f.to_dict() for f in self.findings],
        }


def _dep_present(module: str) -> bool:
    """True if ``module`` is importable, checked WITHOUT importing it (so the
    probe never drags a heavy dep onto the cold path)."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):  # pragma: no cover - defensive
        return False


def _check_dependencies() -> list[Finding]:
    present = [m for m in _OPTIONAL_DEPS if _dep_present(m)]
    missing = [m for m in _OPTIONAL_DEPS if m not in present]
    findings: list[Finding] = []
    if missing:
        findings.append(Finding(
            check="dependencies",
            severity=SEVERITY_WARNING,
            message=(
                "knowledge extra not fully installed; missing "
                f"{', '.join(missing)}. Ingestion needs "
                "'pip install xpst[knowledge]'. Querying organized data works "
                "without it."
            ),
        ))
    else:
        findings.append(Finding(
            check="dependencies",
            severity=SEVERITY_OK,
            message="knowledge extra fully installed",
        ))
    return findings


def _check_store(store: JsonKnowledgeStore) -> tuple[list[Finding], list, list]:
    nuggets = list(store.all_nuggets())
    areas = store.areas()
    findings: list[Finding] = []
    if not nuggets:
        findings.append(Finding(
            check="store",
            severity=SEVERITY_WARNING,
            message="store is empty (no nuggets ingested yet)",
        ))
    else:
        findings.append(Finding(
            check="store",
            severity=SEVERITY_OK,
            message=f"{len(nuggets)} nuggets, {len(areas)} areas loaded",
        ))
    return findings, nuggets, areas


def _check_embeddings(nuggets: list) -> list[Finding]:
    widths = {len(n.embedding) for n in nuggets if n.embedding}
    embedded = sum(1 for n in nuggets if n.embedding)
    findings: list[Finding] = []
    if len(widths) > 1:
        findings.append(Finding(
            check="embeddings",
            severity=SEVERITY_ERROR,
            message=(
                "mixed embedding widths "
                f"{sorted(widths)} -- the embedding model changed without a "
                "re-embed; centroids/routing are corrupt. Re-embed the "
                "workspace."
            ),
        ))
    elif nuggets and embedded < len(nuggets):
        findings.append(Finding(
            check="embeddings",
            severity=SEVERITY_WARNING,
            message=(
                f"{len(nuggets) - embedded} of {len(nuggets)} nuggets have no "
                "embedding and cannot be routed/recalled"
            ),
        ))
    elif nuggets:
        width = next(iter(widths)) if widths else 0
        findings.append(Finding(
            check="embeddings",
            severity=SEVERITY_OK,
            message=f"all {embedded} nuggets embedded at width {width}",
        ))
    return findings


def _check_orphans(nuggets: list, areas: list) -> list[Finding]:
    findings: list[Finding] = []
    nugget_ids = {n.id for n in nuggets}
    area_ids = {a.id for a in areas}

    empty_areas = [a for a in areas if not a.nugget_ids]
    if empty_areas:
        findings.append(Finding(
            check="orphaned_areas",
            severity=SEVERITY_WARNING,
            message=(
                f"{len(empty_areas)} area(s) have no members "
                f"({', '.join(a.label for a in empty_areas)})"
            ),
        ))

    dangling_members = sum(
        1 for a in areas for nid in a.nugget_ids if nid not in nugget_ids
    )
    if dangling_members:
        findings.append(Finding(
            check="orphaned_areas",
            severity=SEVERITY_ERROR,
            message=(
                f"{dangling_members} area membership reference(s) point at "
                "nuggets that no longer exist; re-run 'kb organize'"
            ),
        ))

    orphan_nuggets = [
        n for n in nuggets
        if n.area_id is not None and n.area_id not in area_ids
    ]
    if orphan_nuggets:
        findings.append(Finding(
            check="orphaned_nuggets",
            severity=SEVERITY_ERROR,
            message=(
                f"{len(orphan_nuggets)} nugget(s) reference a non-existent "
                "area; re-run 'kb organize' to reconcile"
            ),
        ))

    if not empty_areas and not dangling_members and not orphan_nuggets:
        findings.append(Finding(
            check="orphans",
            severity=SEVERITY_OK,
            message="no orphaned areas or nuggets",
        ))
    return findings


def _check_queue(queue: IngestionQueue) -> list[Finding]:
    counts = queue.counts()
    findings: list[Finding] = []
    failed = [it for it in queue.items() if it.status == "failed"]
    if failed:
        sample = "; ".join(
            f"{it.source} ({it.reason})" for it in failed[:3]
        )
        findings.append(Finding(
            check="queue",
            severity=SEVERITY_WARNING,
            message=f"{len(failed)} failed queue item(s): {sample}",
        ))
    findings.append(Finding(
        check="queue",
        severity=SEVERITY_OK,
        message=(
            f"pending={counts['pending']} in_progress={counts['in_progress']} "
            f"done={counts['done']} failed={counts['failed']}"
        ),
    ))
    return findings


def diagnose(workspace: Workspace) -> DoctorReport:
    """Run all read-only health checks against ``workspace`` and return a
    :class:`DoctorReport`. Never mutates store/queue/manifest state."""
    findings: list[Finding] = []
    findings.extend(_check_dependencies())

    store = JsonKnowledgeStore(workspace.nuggets_path)
    store_findings, nuggets, areas = _check_store(store)
    findings.extend(store_findings)
    findings.extend(_check_embeddings(nuggets))
    findings.extend(_check_orphans(nuggets, areas))

    queue = IngestionQueue(workspace.queue_path, readonly=True)
    findings.extend(_check_queue(queue))

    # Touch the manifest so a corrupt one is surfaced (Manifest recovers to
    # empty on corruption rather than raising, so this is informational).
    manifest = Manifest(workspace.manifest_path)
    findings.append(Finding(
        check="manifest",
        severity=SEVERITY_OK,
        message=f"{manifest.source_count()} source(s) recorded",
    ))

    return DoctorReport(workspace=workspace.name, findings=tuple(findings))
