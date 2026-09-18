"""Manual runtime control, available even when bus sensors are asleep."""

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ArrivaCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add a switch for the existing bus device."""
    async_add_entities(
        [
            ArrivaRuntimeSwitch(entry.runtime_data.coordinator),
            ArrivaLiveActivitySwitch(entry.runtime_data.live_activity),
        ]
    )


class ArrivaRuntimeSwitch(CoordinatorEntity[ArrivaCoordinator], SwitchEntity, RestoreEntity):
    """Control the runtime independently of time and presence."""

    _attr_has_entity_name = True
    _attr_name = "Integratie actief"
    _attr_icon = "mdi:bus-clock"

    def __init__(self, coordinator: ArrivaCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry_id}_runtime"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, coordinator.entry_id)})

    @property
    def available(self) -> bool:
        """Network failures must not prevent switching the runtime off."""
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.runtime_active

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        previous = await self.async_get_last_state()
        if previous is None or previous.state == "on":
            await self.coordinator.async_set_enabled(True)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_enabled(False)


class ArrivaLiveActivitySwitch(SwitchEntity, RestoreEntity):
    """Enable or disable Live Activity updates for all selected iPhones."""

    _attr_has_entity_name = True
    _attr_name = "Live Activity"
    _attr_icon = "mdi:cellphone-information"

    def __init__(self, live_activity: Any) -> None:
        self._live_activity = live_activity
        self._attr_unique_id = f"{live_activity.entry_id}_live_activity"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, live_activity.entry_id)})

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        previous = await self.async_get_last_state()
        if previous is not None:
            await self._live_activity.async_set_enabled(previous.state == "on")

    @property
    def available(self) -> bool:
        return self._live_activity.device_count > 0

    @property
    def is_on(self) -> bool:
        return self._live_activity.enabled

    @property
    def extra_state_attributes(self) -> dict[str, int]:
        return {"configured_device_count": self._live_activity.device_count}

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._live_activity.async_set_enabled(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._live_activity.async_set_enabled(False)
        self.async_write_ha_state()
