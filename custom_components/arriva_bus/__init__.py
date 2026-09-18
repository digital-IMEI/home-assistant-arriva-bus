"""Arriva Bus (test) integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import TransitHttpClient
from .catalog import BUNDLED_CATALOG, CatalogError, decode_catalog
from .const import CONF_INITIAL_ACTIVE
from .coordinator import ArrivaCoordinator
from .live_activity import ArrivaLiveActivity
from .route_config import RouteConfig

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.SWITCH]


@dataclass(slots=True)
class ArrivaRuntimeData:
    """Runtime objects owned by one config entry."""

    coordinator: ArrivaCoordinator
    live_activity: ArrivaLiveActivity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Arriva Bus (test) from a config entry."""
    route = RouteConfig.from_data(entry.data)
    try:
        payload = await hass.async_add_executor_job(BUNDLED_CATALOG.read_bytes)
        catalog = await hass.async_add_executor_job(decode_catalog, payload)
        route = replace(
            route,
            destination_aliases=catalog.aliases(
                route.line_planning_number, route.destination, route.target_stop_code
            ),
        )
    except OSError, CatalogError:
        logging.getLogger(__name__).warning(
            "Could not resolve destination aliases; using stored route"
        )
    client = TransitHttpClient(async_get_clientsession(hass), route)
    coordinator = ArrivaCoordinator(hass, entry, client)
    await coordinator.async_start()
    if entry.data.get(CONF_INITIAL_ACTIVE):
        # Activate a newly created tracker before its entities are added. Remove
        # the one-shot marker immediately so a restored off switch stays off on
        # every later Home Assistant restart.
        await coordinator.async_set_enabled(True)
        hass.config_entries.async_update_entry(
            entry,
            data={key: value for key, value in entry.data.items() if key != CONF_INITIAL_ACTIVE},
        )
    live_activity = ArrivaLiveActivity(hass, entry, coordinator)
    entry.runtime_data = ArrivaRuntimeData(coordinator, live_activity)
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        await live_activity.async_start()
    except Exception:
        await live_activity.async_stop()
        await coordinator.async_shutdown()
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        runtime: ArrivaRuntimeData = entry.runtime_data
        await runtime.live_activity.async_stop()
        await runtime.coordinator.async_shutdown()
    return unload_ok
