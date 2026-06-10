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
    def resolve(cls, name: str = "default") -> "Workspace":
        root = _xpst_home() / "knowledge" / name
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
