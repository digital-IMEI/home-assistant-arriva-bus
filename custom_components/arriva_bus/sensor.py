"""Sensors for Arriva Bus (test)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    JOURNEY_STATUS_CANCELLED,
    JOURNEY_STATUS_NO_BUS,
    JOURNEY_STATUS_OPTIONS,
    JOURNEY_STATUS_PREVIOUS_CANCELLED,
    JOURNEY_STATUS_REALTIME_UNAVAILABLE,
    JOURNEY_STATUS_UNDERWAY,
    JOURNEY_STATUS_WAITING,
)
from .coordinator import ArrivaCoordinator
from .models import BusSnapshot

PARALLEL_UPDATES = 0

NO_BUS_UNDERWAY = "Geen bus onderweg"
NOT_DEPARTED = "Nog niet vertrokken"


@dataclass(frozen=True, kw_only=True)
class ArrivaSensorDescription(SensorEntityDescription):
    """Describe a Arriva bus sensor."""

    value_fn: Callable[[BusSnapshot], object | None]
    attrs_fn: Callable[[BusSnapshot], dict[str, object]] | None = None


def _delay_seconds(data: BusSnapshot) -> int | None:
    return data.delay_seconds


def _delay_attrs(data: BusSnapshot) -> dict[str, object]:
    attrs: dict[str, object] = {
        "is_underway": data.is_underway,
        "realtime_connected": data.realtime_connected,
        "realtime_stale": data.realtime_stale,
    }
    if data.delay_seconds is not None:
        attrs["delay_seconds"] = data.delay_seconds
    attrs["raw_delay_seconds"] = data.raw_delay_seconds
    attrs["scheduled_wait_until"] = (
        data.scheduled_wait_until.isoformat() if data.scheduled_wait_until else None
    )
    if data.delay_source_stop:
        attrs["source_stop"] = data.delay_source_stop
    if data.delay_source_status:
        attrs["source_status"] = data.delay_source_status
    if data.delay_observed_at:
        attrs["observed_at"] = data.delay_observed_at.isoformat()
    if data.journey_number is not None:
        attrs["journey_number"] = data.journey_number
    if data.journey_key:
        attrs["journey_key"] = data.journey_key
    return attrs


def position_value(data: BusSnapshot) -> str:
    """Return the current stop, last passed stop or a stable fallback."""
    if data.current_stop:
        return data.current_stop
    if data.last_passed_stop:
        return data.last_passed_stop
    if data.journey_number is not None:
        return NOT_DEPARTED
    return NO_BUS_UNDERWAY


def _position_attrs(data: BusSnapshot) -> dict[str, object]:
    last_known = data.realtime_stale or data.last_journey_cancelled
    if data.current_stop:
        position_type = "last_known_current_stop" if last_known else "current_stop"
    elif data.last_passed_stop:
        position_type = "last_known_passed_stop" if last_known else "last_passed_stop"
    elif data.journey_number is not None:
        position_type = "waiting"
    else:
        position_type = "no_bus"

    attrs: dict[str, object] = {
        "position_type": position_type,
        "is_underway": data.is_underway,
        "realtime_connected": data.realtime_connected,
        "realtime_stale": data.realtime_stale,
    }
    if data.current_stop:
        attrs["current_stop"] = data.current_stop
    if data.current_stop_code:
        attrs["current_stop_code"] = data.current_stop_code
    if data.last_passed_stop:
        attrs["last_passed_stop"] = data.last_passed_stop
    if data.last_passed_stop_code:
        attrs["last_passed_stop_code"] = data.last_passed_stop_code
    if data.last_event_type:
        attrs["last_event_type"] = data.last_event_type
    if data.data_timestamp:
        attrs["data_timestamp"] = data.data_timestamp.isoformat()
    if data.last_received_at:
        attrs["last_received_at"] = data.last_received_at.isoformat()
    return attrs


def journey_status(data: BusSnapshot) -> str:
    """Return a translated enum state without performing any extra work."""
    if data.last_journey_cancelled:
        if data.journey_number is not None:
            return JOURNEY_STATUS_PREVIOUS_CANCELLED
        return JOURNEY_STATUS_CANCELLED
    if data.realtime_stale:
        return JOURNEY_STATUS_REALTIME_UNAVAILABLE
    if data.is_underway:
        return JOURNEY_STATUS_UNDERWAY
    if data.journey_number is not None:
        return JOURNEY_STATUS_WAITING
    return JOURNEY_STATUS_NO_BUS


def _status_attrs(data: BusSnapshot) -> dict[str, object]:
    attrs: dict[str, object] = {
        "realtime_connected": data.realtime_connected,
        "realtime_stale": data.realtime_stale,
    }
    if data.journey_number is not None:
        attrs["journey_number"] = data.journey_number
    if data.journey_key:
        attrs["journey_key"] = data.journey_key
    if data.cancelled_scheduled_time:
        attrs["cancelled_scheduled_time"] = data.cancelled_scheduled_time.isoformat()
    return attrs


SENSORS: tuple[ArrivaSensorDescription, ...] = (
    ArrivaSensorDescription(
        key="delay",
        translation_key="delay",
        icon="mdi:bus-clock",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_display_precision=0,
        value_fn=_delay_seconds,
        attrs_fn=_delay_attrs,
    ),
    ArrivaSensorDescription(
        key="last_passed_stop",
        translation_key="last_passed_stop",
        icon="mdi:bus-stop",
        value_fn=position_value,
        attrs_fn=_position_attrs,
    ),
    ArrivaSensorDescription(
        key="journey_status",
        translation_key="journey_status",
        icon="mdi:bus-alert",
        device_class=SensorDeviceClass.ENUM,
        options=list(JOURNEY_STATUS_OPTIONS),
        value_fn=journey_status,
        attrs_fn=_status_attrs,
    ),
    ArrivaSensorDescription(
        key="scheduled_target",
        translation_key="scheduled_target",
        icon="mdi:clock-outline",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda data: data.target_scheduled_time,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Arriva bus sensors."""
    coordinator: ArrivaCoordinator = entry.runtime_data.coordinator
    async_add_entities(ArrivaSensor(coordinator, description) for description in SENSORS)


class ArrivaSensor(CoordinatorEntity[ArrivaCoordinator], SensorEntity):
    """A sensor backed by the shared Arriva bus coordinator."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ArrivaCoordinator,
        description: ArrivaSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry_id)},
            name=coordinator.route.title,
            manufacturer="Arriva / NDOV",
            model="Realtime NDOV KV6",
        )

    @property
    def available(self) -> bool:
        """Hide all data while sleeping and expose each sensor when meaningful."""
        if not super().available or not self.coordinator.data.runtime_active:
            return False
        if self.entity_description.key in {"last_passed_stop", "journey_status"}:
            return True
        if self.entity_description.key == "scheduled_target":
            return self.coordinator.data.target_scheduled_time is not None
        return (
            self.coordinator.data.is_underway
            and not self.coordinator.data.realtime_stale
            and self.coordinator.data.delay_seconds is not None
        )

    @property
    def native_value(self) -> object | None:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        if self.entity_description.attrs_fn is None:
            return None
        attrs = self.entity_description.attrs_fn(self.coordinator.data)
        if self.coordinator.timing_point_code:
            attrs["target_stop_code"] = self.coordinator.timing_point_code
        if self.coordinator.line_id:
            attrs["ndov_line_id"] = self.coordinator.line_id
        return attrs
