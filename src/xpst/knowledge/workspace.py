"""Per-tenant workspace resolution. No identity is encoded in code —
a workspace is just a name and an isolated directory."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _xpst_home() -> Path:
    return Path(os.environ.get("XPST_HOME", "~/.xpst")).expanduser()


@dataclass(frozen=True)
class Workspace:
    name: str
    root: Path

    @classmethod
    def resolve(cls, name: str = "default", *, create: bool = True) -> Workspace:
        """Resolve a workspace directory. Read paths (query, doctor, areas)
        pass ``create=False`` so probing a nonexistent workspace never
        creates it as a side effect (G30)."""
        root = _xpst_home() / "knowledge" / name
        if create:
            root.mkdir(parents=True, exist_ok=True)
        return cls(name=name, root=root)

    @property
    def nuggets_path(self) -> Path:
        return self.root / "nuggets.json"

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    @property
    def lancedb_path(self) -> Path:
        return self.root / "lancedb"

    @property
    def queue_path(self) -> Path:
        return self.root / "queue.json"

    @property
    def transcripts_dir(self) -> Path:
        return self.root / "transcripts"

    @property
    def vectors_db_path(self) -> Path:
        """SQLite-vec database path (Phase D3 default vector backend)."""
        return self.root / "vectors.db"

    def save_transcript(self, content_hash: str, text: str,
                        segments: list[dict] | None = None) -> Path:
        """Cache a transcript keyed by content_hash (D2.2).

        When the same video is cross-posted, all platforms reference the
        same transcript — no re-transcription needed.
        """
        import json
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)
        path = self.transcripts_dir / f"{content_hash}.json"
        data = {"content_hash": content_hash, "text": text,
                "segments": segments or []}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        return path

    def get_transcript(self, content_hash: str) -> dict | None:
        """Retrieve a cached transcript by content_hash (D2.3).

        Returns ``{"content_hash", "text", "segments"}`` or ``None`` if not cached.
        """
        import json
        path = self.transcripts_dir / f"{content_hash}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except Exception:
            return None
