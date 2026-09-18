"""Config flow for Arriva Bus (test)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, override

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.selector import (
    DeviceSelector,
    DeviceSelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
)

from .catalog import CatalogError, RouteCatalog, async_load_catalog
from .colors import PALETTE, color_choice
from .const import (
    CONF_MOBILE_DEVICE,
    DOMAIN,
)
from .route_config import RouteConfig

MOBILE_APP_DOMAIN = "mobile_app"


def _settings_schema(values: dict[str, Any]) -> vol.Schema:
    """Build selectors while preserving values during reconfiguration."""
    mobile_default = values.get(CONF_MOBILE_DEVICE)
    if isinstance(mobile_default, str):
        mobile_default = [mobile_default]
    mobile_device_key = (
        vol.Optional(CONF_MOBILE_DEVICE, description={"suggested_value": mobile_default})
        if CONF_MOBILE_DEVICE in values
        else vol.Optional(CONF_MOBILE_DEVICE)
    )

    def palette(key, default):
        current = color_choice(values.get(key), default)
        options = ["default", *PALETTE]
        if current not in options:
            options.append(current)
        return SelectSelector(
            SelectSelectorConfig(options=options, mode="dropdown", translation_key="activity_color")
        )

    return vol.Schema(
        {
            vol.Optional(
                "delay_color",
                description={"suggested_value": color_choice(values.get("delay_color"), "red")},
            ): palette("delay_color", "red"),
            vol.Optional(
                "early_color",
                description={"suggested_value": color_choice(values.get("early_color"), "green")},
            ): palette("early_color", "green"),
            vol.Optional(
                "on_time_color",
                description={"suggested_value": color_choice(values.get("on_time_color"), "white")},
            ): palette("on_time_color", "white"),
            mobile_device_key: DeviceSelector(
                DeviceSelectorConfig(
                    multiple=True,
                    filter={
                        "integration": MOBILE_APP_DOMAIN,
                        "manufacturer": "Apple",
                    },
                )
            ),
        }
    )


def _is_iphone(hass: HomeAssistant, device_id: str) -> bool:
    """Return whether a selected device is an Apple iPhone from mobile_app."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return False
    if (device.manufacturer or "").casefold() != "apple":
        return False
    if "iphone" not in (device.model or "").casefold():
        return False
    return any(
        (entry := hass.config_entries.async_get_entry(entry_id)) is not None
        and entry.domain == MOBILE_APP_DOMAIN
        for entry_id in device.config_entries
    )


class ArrivaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Configure an independent Arriva line, destination and stop tracker."""

    VERSION = 1

    @staticmethod
    @callback
    @override
    def async_get_options_flow(config_entry: ConfigEntry) -> ArrivaOptionsFlow:
        """Return settings that update delivery without restarting the tracker."""
        return ArrivaOptionsFlow()

    def __init__(self) -> None:
        self._catalog: RouteCatalog | None = None
        self._planning = ""
        self._destination = ""
        self._route: RouteConfig | None = None

    def _select_form(self, step, options, errors=None, field=None) -> ConfigFlowResult:
        return self.async_show_form(
            step_id=step,
            data_schema=vol.Schema(
                {
                    vol.Required(field or step): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                {"value": key, "label": label} for key, label in options.items()
                            ],
                            mode="dropdown",
                        )
                    )
                }
            ),
            errors=errors or {},
        )

    @override
    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        """Start with all mapped Arriva bus lines from the GTFS catalogue."""
        if self._catalog is None:
            try:
                self._catalog = await async_load_catalog(self.hass)
            except CatalogError:
                return self.async_show_form(
                    step_id="user",
                    data_schema=vol.Schema({}),
                    errors={"base": "catalog_unavailable"},
                )
        choices = self._catalog.line_choices()
        errors = {}
        if user_input and "line" in user_input:
            if user_input["line"] in choices:
                self._planning = user_input["line"]
                return await self.async_step_direction()
            errors["base"] = "invalid_selection"
        return self._select_form("user", choices, errors, field="line")

    async def async_step_direction(self, user_input=None) -> ConfigFlowResult:
        choices = self._catalog.directions(self._planning)
        errors = {}
        if user_input is not None:
            if user_input.get("direction") in choices:
                self._destination = user_input["direction"]
                return await self.async_step_stop()
            errors["base"] = "invalid_selection"
        return self._select_form("direction", choices, errors)

    async def async_step_stop(self, user_input=None) -> ConfigFlowResult:
        choices = self._catalog.stops(self._planning, self._destination)
        errors = {}
        if user_input is not None:
            code = user_input.get("stop")
            if code in choices:
                self._route = RouteConfig(
                    self._planning,
                    self._catalog.public_number(self._planning),
                    self._destination,
                    code,
                    choices[code],
                    self._catalog.aliases(self._planning, self._destination, code),
                )
                await self.async_set_unique_id(self._route.unique_id)
                self._abort_if_unique_id_configured()
                return await self.async_step_devices()
            errors["base"] = "invalid_selection"
        return self._select_form("stop", choices, errors)

    async def async_step_devices(self, user_input=None) -> ConfigFlowResult:
        """Optional multi-iPhone delivery; bus tracking also works without it."""
        errors = {}
        if user_input is not None:
            devices = user_input.get(CONF_MOBILE_DEVICE) or []
            if any(not _is_iphone(self.hass, device_id) for device_id in devices):
                errors[CONF_MOBILE_DEVICE] = "invalid_iphone"
            else:
                assert self._route is not None
                await self.async_set_unique_id(self._route.unique_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=self._route.title,
                    data={**asdict(self._route), **user_input, CONF_MOBILE_DEVICE: devices},
                )
        return self.async_show_form(
            step_id="devices",
            data_schema=_settings_schema(user_input or {}),
            errors=errors,
        )


class ArrivaOptionsFlow(OptionsFlow):
    """Allow existing installations to select or change iPhones."""

    @override
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Update private settings and live delivery in place."""
        if user_input is not None:
            mobile_devices = user_input.get(CONF_MOBILE_DEVICE) or []
            if isinstance(mobile_devices, str):
                mobile_devices = [mobile_devices]
            if any(not _is_iphone(self.hass, device_id) for device_id in mobile_devices):
                return self.async_show_form(
                    step_id="init",
                    data_schema=_settings_schema(user_input),
                    errors={CONF_MOBILE_DEVICE: "invalid_iphone"},
                )
            # Explicit resets override values originally saved in entry.data.
            options = {**user_input, CONF_MOBILE_DEVICE: mobile_devices}
            for key in ("delay_color", "early_color", "on_time_color"):
                options[key] = user_input.get(key) or "default"
            runtime = getattr(self.config_entry, "runtime_data", None)
            if runtime is not None:
                try:
                    await runtime.live_activity.async_update_settings(options)
                except HomeAssistantError:
                    return self.async_show_form(
                        step_id="init",
                        data_schema=_settings_schema(options),
                        errors={"base": "device_clear_failed"},
                    )
            return self.async_create_entry(data=options)

        return self.async_show_form(
            step_id="init",
            data_schema=_settings_schema({**self.config_entry.data, **self.config_entry.options}),
        )
