"""LLM nugget extraction over a strict JSON schema. The model only does a
narrow, schema-bound transform (transcript -> key points); the organizing
intelligence lives elsewhere in the pipeline. One repair retry is attempted on a
malformed response, then ``ExtractionError`` is raised. Timestamps are clamped
to the transcript's own bounds so a hallucinated time can never escape the clip.
"""
from __future__ import annotations

from typing import Any, Protocol

from xpst.knowledge.ingest.transcribe import Transcript


class ExtractionError(RuntimeError):
    """Raised when the LLM fails to return schema-valid nuggets after a retry."""


class _Chatter(Protocol):
    def chat_json(self, messages: list[dict[str, Any]]) -> dict: ...


_SYSTEM = (
    "You extract the key teachable points from a video transcript. "
    "Return ONLY a JSON object of the form "
    '{"nuggets": [{"point": str, "timestamp_start": number, '
    '"timestamp_end": number}]}. '
    "Each 'point' is one self-contained idea in 1-3 sentences. "
    "Timestamps are seconds into the video and must lie within the "
    "transcript's start and end. Do not invent content not in the transcript. "
    "If there are no teachable points, return an empty nuggets list."
)


def _user_prompt(transcript: Transcript) -> str:
    lines = [
        f"[{s.start:.1f}-{s.end:.1f}] {s.text}" for s in transcript.segments
    ]
    body = "\n".join(lines) if lines else transcript.text
    return (
        f"Transcript spans {transcript.start:.1f}s to {transcript.end:.1f}s.\n"
        f"Transcript:\n{body}"
    )


def _validate(payload: Any) -> list[dict[str, Any]]:
    """Return the raw nugget dicts if the payload matches the schema, else
    raise ValueError so the caller can decide to retry."""
    if not isinstance(payload, dict):
        raise ValueError("response is not a JSON object")
    nuggets = payload.get("nuggets")
    if not isinstance(nuggets, list):
        raise ValueError("'nuggets' must be a list")
    out: list[dict[str, Any]] = []
    for item in nuggets:
        if not isinstance(item, dict):
            raise ValueError("each nugget must be an object")
        point = item.get("point")
        if not isinstance(point, str) or not point.strip():
            raise ValueError("each nugget needs a non-empty string 'point'")
        try:
            ts_start = float(item.get("timestamp_start", 0.0))
            ts_end = float(item.get("timestamp_end", 0.0))
        except (TypeError, ValueError) as exc:
            raise ValueError("timestamps must be numbers") from exc
        out.append({
            "point": point.strip(),
            "timestamp_start": ts_start,
            "timestamp_end": ts_end,
        })
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
                    client: _Chatter) -> list[dict[str, Any]]:
    """Extract nugget dicts from ``transcript`` using ``client``.

    Returns a list of ``{point, timestamp_start, timestamp_end}`` dicts with
    timestamps clamped to the transcript bounds. Attempts one repair retry on a
    schema-invalid response, then raises :class:`ExtractionError`.
    """
    messages = [
        {"role": "system", "content": _SYSTEM},
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
            validated = _validate(payload)
            return _clamp(validated, transcript.start, transcript.end)
        except (ValueError, KeyError, TypeError) as exc:
            last_error = exc
    raise ExtractionError(
        f"LLM did not return schema-valid nuggets after a retry: {last_error}"
    )
