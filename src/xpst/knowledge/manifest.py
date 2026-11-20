"""Per-workspace ingestion manifest. Tracks which sources have been ingested
(content-hash dedup at the source level) plus the embedding model/dimension in
force, so changing the embedding model can trigger a re-embed. Writes are atomic
(tempfile + os.replace), mirroring ``src/xpst/state_store.py``."""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


class Manifest:
    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return self._ensure_keys(data)
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                # Corrupt manifest: start fresh rather than crash the pipeline.
                pass
        return self._empty()

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "schema_version": Manifest.SCHEMA_VERSION,
            "sources": {},
            "aliases": {},
        }

    def _ensure_keys(self, data: dict[str, Any]) -> dict[str, Any]:
        data.setdefault("schema_version", self.SCHEMA_VERSION)
        data.setdefault("sources", {})
        data.setdefault("aliases", {})
        return data

    def _atomic_write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=self.path.parent,
            prefix=f"{self.path.name}.tmp.",
            delete=False,
            encoding="utf-8",
        ) as tmp:
            json.dump(self._data, tmp, indent=2, ensure_ascii=False)
            tmp_path = Path(tmp.name)
        try:
            os.replace(tmp_path, self.path)
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink()
            raise

    def has_source(self, source_video_id: str) -> bool:
        return (
            source_video_id in self._data["sources"]
            or source_video_id in self._data["aliases"]
        )

    def record_alias(self, alias_id: str, canonical_id: str) -> None:
        """Map a secondary dedup key (e.g. a content-byte fingerprint) onto a
        recorded source so the same media reached via a different source
        string is never re-ingested (G33). Aliases do not inflate
        ``source_count``."""
        self._data["aliases"][alias_id] = canonical_id
        self._atomic_write()

    def source_count(self) -> int:
        """Number of distinct sources recorded. Read-only."""
        return len(self._data["sources"])

    def record(self, source_video_id: str, *, source: str | None,
               embed_model: str, embed_dim: int) -> None:
        """Record (or refresh) that a source has been ingested. Idempotent on
        ``source_video_id``."""
        self._data["sources"][source_video_id] = {
            "source": source,
            "embed_model": embed_model,
            "embed_dim": embed_dim,
            "recorded_at": time.time(),
        }
        self._atomic_write()
