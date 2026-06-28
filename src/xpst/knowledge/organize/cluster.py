"""Area discovery (clustering) + auto-labeling.

Clustering is a dependency-light greedy/threshold pass over the nugget
embeddings -- no numpy, no scikit, no new runtime deps (spec §7). It is
order-independent: nuggets are visited in a canonical order (by id) and each
joins the existing cluster whose centroid is the most similar above
``threshold``, otherwise it seeds a new cluster. Centroids are recomputed as
members join, so the result is deterministic given the inputs.

Auto-labeling asks the existing OpenAI-compatible LLM (behind the same
``_Chatter`` protocol the extractor uses) for a SHORT area label, with strict
validation + one repair retry + a deterministic graceful fallback so a dead or
misbehaving model can never crash organization (spec §5)."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from xpst.knowledge.models import Area
from xpst.knowledge.organize._vectors import centroid as _centroid
from xpst.knowledge.organize._vectors import cosine
from xpst.knowledge.organize.difficulty import area_difficulty_rank

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from xpst.knowledge.models import Nugget


class _Chatter(Protocol):
    def chat_json(self, messages: list[dict[str, Any]]) -> dict: ...


# Match the router default so a freshly-discovered area and incremental routing
# use the same notion of "close enough".
DEFAULT_CLUSTER_THRESHOLD = 0.6
_MAX_LABEL_LEN = 60
_LABEL_PREVIEW_POINTS = 6


# ── clustering ────────────────────────────────────────────────────────────

class _Cluster:
    __slots__ = ("members", "centroid")

    def __init__(self, first: Nugget) -> None:
        self.members: list[Nugget] = [first]
        self.centroid: tuple[float, ...] = tuple(first.embedding)

    def add(self, nugget: Nugget) -> None:
        self.members.append(nugget)
        self.centroid = _centroid([m.embedding for m in self.members])


def cluster_nuggets(nuggets: Sequence[Nugget], *,
                    threshold: float = DEFAULT_CLUSTER_THRESHOLD,
                    ) -> list[list[Nugget]]:
    """Group embedded ``nuggets`` into clusters by greedy threshold.

    Nuggets without an embedding are dropped (they cannot be placed by
    similarity). Returns a list of member lists. Deterministic regardless of
    input order: nuggets are processed sorted by id, and ties between candidate
    clusters break on the cluster's lead member id."""
    embedded = sorted((n for n in nuggets if n.embedding), key=lambda n: n.id)
    clusters: list[_Cluster] = []
    for nugget in embedded:
        best: _Cluster | None = None
        best_sim = -1.0
        for cluster in clusters:
            sim = cosine(nugget.embedding, cluster.centroid)
            if sim > best_sim or (sim == best_sim and best is not None
                                  and cluster.members[0].id < best.members[0].id):
                best, best_sim = cluster, sim
        if best is not None and best_sim >= threshold:
            best.add(nugget)
        else:
            clusters.append(_Cluster(nugget))
    return [c.members for c in clusters]


# ── auto-labeling ──────────────────────────────────────────────────────────

_SYSTEM = (
    "You name a course module from a few related teaching points. "
    'Return ONLY a JSON object of the form {"label": str}. '
    "The label is 2-5 words, title-case, no trailing punctuation. "
    "Do not add any other keys or prose."
)


def _label_prompt(cluster: Sequence[Nugget]) -> str:
    points = [n.point for n in cluster[:_LABEL_PREVIEW_POINTS]]
    body = "\n".join(f"- {p}" for p in points)
    return f"Teaching points in this module:\n{body}\n\nName the module."


def _validate_label(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise ValueError("response is not a JSON object")
    label = payload.get("label")
    if not isinstance(label, str) or not label.strip():
        raise ValueError("'label' must be a non-empty string")
    cleaned = " ".join(label.split())[:_MAX_LABEL_LEN].strip()
    if not cleaned:
        raise ValueError("label empty after cleaning")
    return cleaned


def _fallback_label(cluster: Sequence[Nugget]) -> str:
    """Deterministic label from the lead nugget's text when the LLM is
    unavailable -- the pipeline must keep working offline / on a dead model."""
    if not cluster:
        return "Untitled Area"
    lead = min(cluster, key=lambda n: n.id)
    words = lead.point.split()
    label = " ".join(words[:6]).strip().rstrip(".,;:")
    return label[:_MAX_LABEL_LEN] or "Untitled Area"


def label_cluster(cluster: Sequence[Nugget], client: _Chatter) -> str:
    """Return a short label for ``cluster``.

    Tries the LLM with one repair retry; on any failure (invalid response twice,
    or the client raising) returns a deterministic fallback derived from the
    cluster content. Never raises."""
    if not cluster:
        return "Untitled Area"
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": _label_prompt(cluster)},
    ]
    last_error: Exception | None = None
    for attempt in range(2):  # initial attempt + one repair retry
        if attempt == 1:
            messages = messages + [{
                "role": "user",
                "content": (
                    f"Your previous response was invalid: {last_error}. "
                    'Reply again with ONLY {"label": "..."}.'
                ),
            }]
        try:
            payload = client.chat_json(messages)
            return _validate_label(payload)
        except (ValueError, KeyError, TypeError) as exc:
            last_error = exc
        except Exception as exc:  # noqa: BLE001 - a dead LLM must not crash organize
            last_error = exc
            break
    return _fallback_label(cluster)


# ── discovery (clustering + labeling + centroids + ordering) ────────────────

def discover_areas(nuggets: Sequence[Nugget], client: _Chatter, *,
                   threshold: float = DEFAULT_CLUSTER_THRESHOLD) -> list[Area]:
    """Cluster ``nuggets``, label each cluster, and build :class:`Area` objects
    carrying centroid, member ids, and a beginner->advanced ``order_index``.

    Deterministic: clusters are sorted by mean difficulty rank then lead-member
    id before order indices are assigned, so the same corpus always produces the
    same ordered areas regardless of input order."""
    clusters = cluster_nuggets(nuggets, threshold=threshold)
    if not clusters:
        return []

    ordered = sorted(
        clusters,
        key=lambda c: (area_difficulty_rank(c), min(n.id for n in c)),
    )
    areas: list[Area] = []
    for order_index, members in enumerate(ordered):
        label = label_cluster(members, client)
        areas.append(Area.create(
            label=label,
            centroid=_centroid([m.embedding for m in members]),
            nugget_ids=[m.id for m in members],
            order_index=order_index,
        ))
    return areas
