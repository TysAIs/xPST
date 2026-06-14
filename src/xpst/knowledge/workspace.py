"""Per-tenant workspace resolution. No identity is encoded in code —
a workspace is just a name and an isolated directory."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from xpst.utils.platform import get_config_dir


def _xpst_home() -> Path:
    if override := os.environ.get("XPST_HOME"):
        return Path(override).expanduser()
    return get_config_dir()


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
