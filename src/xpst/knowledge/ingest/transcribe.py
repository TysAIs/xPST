"""Transcription. faster_whisper is imported lazily inside the adapter so the
package can be imported without the heavy dependency present."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Transcript:
    text: str
    segments: list[Segment]

    @property
    def start(self) -> float:
        return self.segments[0].start if self.segments else 0.0

    @property
    def end(self) -> float:
        return self.segments[-1].end if self.segments else 0.0


class Transcriber(Protocol):
    def transcribe(self, media_path: Path) -> Transcript: ...


class FasterWhisperTranscriber:
    """Adapter over faster-whisper. Model loads lazily on first use."""

    def __init__(self, model_size: str = "base") -> None:
        self._model_size = model_size
        self._model = None

    def _ensure_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel  # lazy, heavy
            self._model = WhisperModel(self._model_size, device="cpu",
                                       compute_type="int8")
        return self._model

    def transcribe(self, media_path: Path) -> Transcript:
        model = self._ensure_model()
        segments, _info = model.transcribe(str(media_path))
        segs = [Segment(start=s.start, end=s.end, text=s.text.strip())
                for s in segments]
        text = " ".join(s.text for s in segs).strip()
        return Transcript(text=text, segments=segs)
