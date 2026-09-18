"""Immutable route identity, kept separate for every configured tracker."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class RouteConfig:
    """Route identity discovered from GTFS, or an existing departure-board setup."""

    line_planning_number: str
    line_public_number: str
    destination: str
    target_stop_code: str
    target_stop_name: str
    destination_aliases: tuple[str, ...] = ()

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> RouteConfig:
        return cls(**{field: data[field] for field in cls.__dataclass_fields__ if field in data})

    @property
    def title(self) -> str:
        return f"Bus {self.line_public_number} → {self.destination} · {self.target_stop_name}"

    @property
    def unique_id(self) -> str:
        return (
            f"ARR:{self.line_planning_number}:{self.target_stop_code}:{self.destination.casefold()}"
        )
