"""Stable KnowledgeStore interface. Volatile backends (JSON now, LanceDB +
Graphify later) implement this so swapping one means rewriting one adapter."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable

from xpst.knowledge.models import Nugget


class KnowledgeStore(ABC):
    @abstractmethod
    def add_nugget(self, nugget: Nugget) -> None: ...

    @abstractmethod
    def get_nugget(self, nugget_id: str) -> Nugget | None: ...

    @abstractmethod
    def has_nugget(self, nugget_id: str) -> bool: ...

    @abstractmethod
    def all_nuggets(self) -> Iterable[Nugget]: ...
