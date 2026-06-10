"""Stable KnowledgeStore interface. Volatile backends (JSON now, LanceDB +
Graphify later) implement this so swapping one means rewriting one adapter."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from xpst.knowledge.models import Area, Nugget


class KnowledgeStore(ABC):
    @abstractmethod
    def add_nugget(self, nugget: Nugget) -> None: ...

    @abstractmethod
    def get_nugget(self, nugget_id: str) -> Nugget | None: ...

    @abstractmethod
    def has_nugget(self, nugget_id: str) -> bool: ...

    @abstractmethod
    def all_nuggets(self) -> Iterable[Nugget]: ...

    @abstractmethod
    def search(self, embedding: Sequence[float], k: int) -> list[Nugget]:
        """Return the ``k`` nuggets nearest ``embedding`` (vector recall),
        best match first. Nuggets without an embedding are ignored."""
        ...

    @abstractmethod
    def upsert_area(self, area: Area) -> None:
        """Insert or replace an area by its id."""
        ...

    @abstractmethod
    def remove_area(self, area_id: str) -> None:
        """Remove an area by id. No-op if absent. Stored nuggets are not
        modified here; ``organize_store`` clears the ``area_id`` of any nugget
        left pointing at a removed area on its next run."""
        ...

    @abstractmethod
    def areas(self) -> list[Area]:
        """Return all areas, ordered by ``order_index`` then label."""
        ...

    @abstractmethod
    def assign(self, nugget_id: str, area_id: str | None) -> None:
        """Assign a stored nugget to an area, or clear its area when ``area_id``
        is None. No-op if the nugget is absent."""
        ...

    @abstractmethod
    def set_difficulty(self, nugget_id: str, difficulty: str) -> None:
        """Persist a difficulty tag on a stored nugget. No-op if absent."""
        ...
