"""LLM extraction over a strict JSON schema, generalized in Phase 4 to three
modes: ``nuggets`` (the classic teachable-points transform), ``clips``
(repurposable clip-worthy moments with a title/topic/tags), and ``topics``
(topic summaries). The model only does a narrow, schema-bound transform
(transcript -> structured items); the organizing intelligence lives elsewhere
in the pipeline. One repair retry is attempted on a malformed response, then
``ExtractionError`` is raised. Timestamps are clamped to the transcript's own
bounds so a hallucinated time can never escape the clip.

A deterministic, LLM-free extractor (:func:`deterministic_extract`) is also
provided so the knowledge base works out-of-box with no LLM configured: it
segments the transcript and pulls keyword topics/tags locally.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from xpst.knowledge.ingest.transcribe import Transcript


class ExtractionError(RuntimeError):
    """Raised when the LLM fails to return schema-valid items after a retry."""


class _Chatter(Protocol):
    def chat_json(self, messages: list[dict[str, Any]]) -> dict: ...


# ── extraction modes ────────────────────────────────────────────────────────

EXTRACT_NUGGETS = "nuggets"
EXTRACT_CLIPS = "clips"
EXTRACT_TOPICS = "topics"
_EXTRACT_MODES = frozenset({EXTRACT_NUGGETS, EXTRACT_CLIPS, EXTRACT_TOPICS})

_SYSTEM_NUGGETS = (
    "You extract the key teachable points from a video transcript. "
    "Return ONLY a JSON object of the form "
    '{"nuggets": [{"point": str, "timestamp_start": number, '
    '"timestamp_end": number}]}. '
    "Each 'point' is one self-contained idea in 1-3 sentences. "
    "Timestamps are seconds into the video and must lie within the "
    "transcript's start and end. Do not invent content not in the transcript. "
    "If there are no teachable points, return an empty nuggets list."
)

_SYSTEM_CLIPS = (
    'You extract repurposable, clip-worthy moments from a video transcript -- '
    'short segments a creator could re-edit into a new post. Return ONLY a '
    'JSON object of the form {"clips": [{"title": str, "topic": str, '
    '"tags": [str], "point": str, "timestamp_start": number, '
    '"timestamp_end": number}]}. "title" is a short hook (<=8 words), "topic" '
    'is the subject, "tags" are 1-5 lowercase keywords, and "point" is a '
    'is a 1-2 sentence summary of why the clip is worth reusing. Timestamps '
    'are seconds into the video and must lie within the transcript start and '
    'end. Do not invent content not in the transcript. If there are no '
    'clip-worthy moments, return an empty clips list.'
)

_SYSTEM_TOPICS = (
    'You extract topic summaries from a video transcript -- the subjects the '
    'video covers, each with a one-sentence summary. Return ONLY a JSON '
    'object of the form {"topics": [{"topic": str, "summary": str, '
    '"tags": [str], "timestamp_start": number, "timestamp_end": number}]}. '
    '"topic" is a short subject label, "summary" is one self-contained '
    'sentence, "tags" are 1-5 lowercase keywords. Timestamps are seconds '
    'into the video and must lie within the transcript start and end. Do '
    'not invent content not in the transcript. If there are no topics, '
    'return an empty topics list.'
)

_SYSTEM_BY_MODE = {
    EXTRACT_NUGGETS: _SYSTEM_NUGGETS,
    EXTRACT_CLIPS: _SYSTEM_CLIPS,
    EXTRACT_TOPICS: _SYSTEM_TOPICS,
}

_KEY_BY_MODE = {
    EXTRACT_NUGGETS: "nuggets",
    EXTRACT_CLIPS: "clips",
    EXTRACT_TOPICS: "topics",
}


def _user_prompt(transcript: Transcript) -> str:
    lines = [
        f"[{s.start:.1f}-{s.end:.1f}] {s.text}" for s in transcript.segments
    ]
    body = "\n".join(lines) if lines else transcript.text
    return (
        f"Transcript spans {transcript.start:.1f}s to {transcript.end:.1f}s.\n"
        f"Transcript:\n{body}"
    )


def _as_tags(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        return tuple(t.strip() for t in value.split(",") if t.strip())
    try:
        return tuple(str(t).strip() for t in value if str(t).strip())
    except TypeError:
        return ()


def _validate(payload: Any, mode: str) -> list[dict[str, Any]]:
    """Return the raw item dicts if the payload matches the schema for ``mode``,
    else raise ValueError so the caller can decide to retry."""
    if not isinstance(payload, dict):
        raise ValueError("response is not a JSON object")
    key = _KEY_BY_MODE[mode]
    items = payload.get(key)
    if not isinstance(items, list):
        raise ValueError(f"'{key}' must be a list")
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("each item must be an object")
        # "point" is the common denominator. nuggets require it directly; for
        # clips it is the summary, for topics it falls back to the summary/
        # topic text so downstream (which reads `point`) always has text.
        point = item.get("point")
        if mode == EXTRACT_NUGGETS:
            if not isinstance(point, str) or not point.strip():
                raise ValueError("each nugget needs a non-empty string 'point'")
            point = point.strip()
        else:
            summary = item.get("summary") or item.get("point")
            if not isinstance(summary, str) or not summary.strip():
                raise ValueError(
                    f"each {mode[:-1]} needs a non-empty 'summary'/'point'"
                )
            point = summary.strip()
        try:
            ts_start = float(item.get("timestamp_start", 0.0))
            ts_end = float(item.get("timestamp_end", 0.0))
        except (TypeError, ValueError) as exc:
            raise ValueError("timestamps must be numbers") from exc
        entry: dict[str, Any] = {
            "point": point,
            "timestamp_start": ts_start,
            "timestamp_end": ts_end,
        }
        # Optional richer metadata (clips/topics modes).
        title = item.get("title")
        if isinstance(title, str) and title.strip():
            entry["title"] = title.strip()
        topic = item.get("topic")
        if isinstance(topic, str) and topic.strip():
            entry["topic"] = topic.strip()
        tags = _as_tags(item.get("tags"))
        if tags:
            entry["tags"] = tags
        out.append(entry)
    return out


def _clamp(nuggets: list[dict[str, Any]],
           lo: float, hi: float) -> list[dict[str, Any]]:
    clamped: list[dict[str, Any]] = []
    for n in nuggets:
        start = min(max(n["timestamp_start"], lo), hi)
        end = min(max(n["timestamp_end"], lo), hi)
        if end < start:
            end = start
        clamped.append({**n, "timestamp_start": start, "timestamp_end": end})
    return clamped


def extract_nuggets(transcript: Transcript,
                    client: _Chatter,
                    mode: str = EXTRACT_NUGGETS) -> list[dict[str, Any]]:
    """Extract item dicts from ``transcript`` using ``client``.

    ``mode`` selects the extraction prompt/schema: ``nuggets`` (teachable
    points, the default and historical behavior), ``clips`` (repurposable
    clip-worthy moments), or ``topics`` (topic summaries). Returns a list of
    ``{point, timestamp_start, timestamp_end[, title, topic, tags]}`` dicts
    with timestamps clamped to the transcript bounds. The ``nuggets`` mode
    returns exactly the classic ``{point, timestamp_start, timestamp_end}``
    shape so existing callers and tests are unaffected.

    Attempts one repair retry on a schema-invalid response, then raises
    :class:`ExtractionError`.
    """
    if mode not in _EXTRACT_MODES:
        raise ValueError(f"unknown extraction mode: {mode!r}")
    messages = [
        {"role": "system", "content": _SYSTEM_BY_MODE[mode]},
        {"role": "user", "content": _user_prompt(transcript)},
    ]
    last_error: Exception | None = None
    for attempt in range(2):  # initial attempt + one repair retry
        if attempt == 1:
            messages = messages + [{
                "role": "user",
                "content": (
                    "Your previous response was invalid: "
                    f"{last_error}. Reply again with ONLY the JSON object "
                    "described, nothing else."
                ),
            }]
        try:
            payload = client.chat_json(messages)
            validated = _validate(payload, mode)
            return _clamp(validated, transcript.start, transcript.end)
        except (ValueError, KeyError, TypeError) as exc:
            last_error = exc
    raise ExtractionError(
        f"LLM did not return schema-valid {mode} after a retry: {last_error}"
    )


# ── deterministic (LLM-free) fallback ────────────────────────────────────────

# A small English stopword list keeps the fallback dependency-free; keyword
# extraction is deliberately crude -- it only needs to be deterministic and
# useful, not smart.
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "if", "then", "else", "when", "of",
    "to", "in", "on", "for", "with", "without", "is", "are", "was", "were",
    "be", "been", "being", "this", "that", "these", "those", "it", "its",
    "as", "at", "by", "from", "into", "than", "so", "such", "too",
    "very", "can", "will", "just", "should", "now", "you", "we", "they",
    "he", "she", "your", "our", "their", "my", "me", "us", "them", "what",
    "which", "who", "how", "why", "where", "there", "here", "about", "up",
    "out", "do", "does", "did", "has", "have", "had", "not", "no", "yes",
})
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]+")
_SENT_RE = re.compile(r"[^.!?\n]+[.!?]?")


def _keywords(text: str, limit: int = 3) -> list[str]:
    counts: dict[str, int] = {}
    for m in _WORD_RE.finditer(text):
        w = m.group(0).lower()
        if len(w) < 3 or w in _STOPWORDS:
            continue
        counts[w] = counts.get(w, 0) + 1
    return [w for w, _ in sorted(
        counts.items(), key=lambda kv: (-kv[1], kv[0])
    )[:limit]]


def _split_sentences(text: str) -> list[str]:
    sents = [s.strip() for s in _SENT_RE.findall(text) if s.strip()]
    return sents or ([text.strip()] if text.strip() else [])


def deterministic_extract(transcript: Transcript,
                          mode: str = EXTRACT_NUGGETS) -> list[dict[str, Any]]:
    """LLM-free extractor: segment the transcript and derive a nugget per
    sentence/segment with locally-extracted topic/tags.

    This is the out-of-box fallback used when no LLM is configured or the
    configured LLM is unreachable (Phase 4.4). It is deterministic and
    dependency-free. In ``nuggets`` mode it emits classic
    ``{point, timestamp_start, timestamp_end}`` dicts; in ``clips``/``topics``
    modes it additionally fills ``title``/``topic``/``tags`` from keyword
    extraction so the richer fields are populated without an LLM.
    """
    if mode not in _EXTRACT_MODES:
        raise ValueError(f"unknown extraction mode: {mode!r}")

    segments = transcript.segments or []
    out: list[dict[str, Any]] = []
    if not segments:
        return out

    lo = transcript.start
    hi = transcript.end
    for seg in segments:
        for sent in _split_sentences(seg.text):
            if not sent:
                continue
            kws = _keywords(sent)
            entry: dict[str, Any] = {
                "point": sent,
                "timestamp_start": min(max(seg.start, lo), hi),
                "timestamp_end": min(max(seg.end, lo), hi),
            }
            if mode != EXTRACT_NUGGETS:
                entry["title"] = sent[:60]
                entry["topic"] = kws[0] if kws else "general"
                if kws:
                    entry["tags"] = tuple(kws)
            out.append(entry)
    return out
