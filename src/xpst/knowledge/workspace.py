"""Per-tenant workspace resolution. No identity is encoded in code —
a workspace is just a name and an isolated directory."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

# Safe workspace/identifier charset: alphanumeric, dash, underscore, colon, dot
# Rejects path separators (/, \\), leading ~, and parent traversal (..)
_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9:_\-\.]+$")


def _validate_name(name: str, field: str = "name") -> str:
    """Validate that a workspace name or content hash is path-safe.

    Rejects path separators, parent traversal, and other dangerous characters
    that could escape the workspace root directory.
    """
    if not name or not _SAFE_NAME_RE.match(name):
        raise ValueError(
            f"Invalid {field}: must contain only alphanumeric, dash, underscore, "
            f"colon, or dot characters (got: {name!r})"
        )
    if ".." in name:
        raise ValueError(f"Invalid {field}: parent traversal (..) is not allowed")
    if name.startswith("~"):
        raise ValueError(f"Invalid {field}: leading tilde is not allowed")
    return name


def _validate_content_hash(content_hash: str) -> str:
    """Validate that a content hash is safe to use as a filename."""
    return _validate_name(content_hash, "content_hash")


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
        _validate_name(name, "workspace name")
        root = _xpst_home() / "knowledge" / name
        # Containment check: ensure resolved path is within the knowledge root
        knowledge_root = (_xpst_home() / "knowledge").resolve()
        if not root.resolve().is_relative_to(knowledge_root):
            raise ValueError(
                f"Workspace path escapes knowledge root: {root} is not within {knowledge_root}"
            )
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
        _validate_content_hash(content_hash)
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)
        path = self.transcripts_dir / f"{content_hash}.json"
        # Containment check
        if not path.resolve().is_relative_to(self.transcripts_dir.resolve()):
            raise ValueError("Transcript path escapes transcripts directory")
        data = {"content_hash": content_hash, "text": text,
                "segments": segments or []}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        return path

    def get_transcript(self, content_hash: str) -> dict | None:
        """Retrieve a cached transcript by content_hash (D2.3).

        Returns ``{"content_hash", "text", "segments"}`` or ``None`` if not cached.
        """
        import json
        _validate_content_hash(content_hash)
        path = self.transcripts_dir / f"{content_hash}.json"
        # Containment check
        if not path.resolve().is_relative_to(self.transcripts_dir.resolve()):
            raise ValueError("Transcript path escapes transcripts directory")
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except Exception:
            return None
