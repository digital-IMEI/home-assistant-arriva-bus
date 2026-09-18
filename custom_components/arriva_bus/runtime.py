"""Pure runtime policy helpers for Arriva Bus (test)."""

from __future__ import annotations

from .const import MAX_EARLY_SECONDS

TRIP_START_EVENTS = frozenset({"DEPARTURE", "ONROUTE"})


def should_reject_implausibly_early(
    event_type: str,
    punctuality: int | None,
    *,
    trip_underway: bool,
) -> bool:
    """Reject a >10 minute lead only once an event proves trip progress."""
    return (
        punctuality is not None
        and punctuality < -MAX_EARLY_SECONDS
        and (trip_underway or event_type in TRIP_START_EVENTS)
    )
