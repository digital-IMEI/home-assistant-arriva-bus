"""Diagnostics for Arriva Bus (test)."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_MOBILE_DEVICE,
    JOURNEY_DISCOVERY_INTERVAL,
    JOURNEY_REVALIDATE_INTERVAL,
    MAINTENANCE_INTERVAL,
)
from .coordinator import ArrivaCoordinator


def _serialize(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return compact diagnostics without dumping raw public-transport data."""
    coordinator: ArrivaCoordinator = entry.runtime_data.coordinator
    snapshot = coordinator.data
    return {
        "line_id": coordinator.line_id,
        "target_timing_point_code": coordinator.timing_point_code,
        "runtime": {
            "active": coordinator.runtime_active,
            "enabled": coordinator.enabled,
            "inactive_reason": coordinator.inactive_reason,
            "live_activity_configured": bool(
                {
                    **entry.data,
                    **entry.options,
                }.get(CONF_MOBILE_DEVICE)
            ),
            "maintenance_interval_seconds": MAINTENANCE_INTERVAL.total_seconds(),
            "journey_discovery_interval_seconds": (JOURNEY_DISCOVERY_INTERVAL.total_seconds()),
            "journey_revalidate_interval_seconds": (JOURNEY_REVALIDATE_INTERVAL.total_seconds()),
        },
        "journey_selection": {
            "selected_at": _serialize(coordinator.active_selected_at),
            "last_success_at": _serialize(coordinator.last_drgl_success_at),
            "error": coordinator.selection_error,
        },
        "realtime": {
            "connected": coordinator.realtime_connected,
            "last_frame_received_at": _serialize(coordinator.last_frame_received_at),
        },
        "stop_mapping": {
            "entries": coordinator.stop_mapping_count,
            "loaded_for": _serialize(coordinator.psa_loaded_for),
            "stale": coordinator.psa_is_stale,
        },
        "snapshot": _serialize(asdict(snapshot)) if snapshot is not None else None,
        "live_activity": entry.runtime_data.live_activity.diagnostics,
    }
