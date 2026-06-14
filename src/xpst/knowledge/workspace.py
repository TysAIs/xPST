"""Per-tenant workspace resolution. No identity is encoded in code —
a workspace is just a name and an isolated directory."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from xpst.utils.platform import get_config_dir

_WORKSPACE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

def _xpst_home(home: str | Path | None = None) -> Path:
    if home is not None:
        return Path(home).expanduser()
    if override := os.environ.get("XPST_HOME"):
        return Path(override).expanduser()
    return get_config_dir()


def _validate_workspace_name(name: str) -> str:
    normalized = str(name).strip()
    if (
        not normalized
        or normalized in {".", ".."}
        or "/" in normalized
        or "\\" in normalized
        or Path(normalized).is_absolute()
        or not _WORKSPACE_NAME_RE.fullmatch(normalized)
    ):
        raise ValueError(
            "workspace must be a simple name using letters, numbers, '.', '_', or '-'"
        )
    return normalized


@dataclass(frozen=True)
class Workspace:
    name: str
    root: Path

    @classmethod
    def resolve(
        cls,
        name: str = "default",
        *,
        create: bool = True,
        home: str | Path | None = None,
    ) -> Workspace:
        """Resolve a workspace directory. Read paths (query, doctor, areas)
        pass ``create=False`` so probing a nonexistent workspace never
        creates it as a side effect (G30)."""
        workspace_name = _validate_workspace_name(name)
        root = _xpst_home(home) / "knowledge" / workspace_name
        if create:
            root.mkdir(parents=True, exist_ok=True)
        return cls(name=workspace_name, root=root)

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
