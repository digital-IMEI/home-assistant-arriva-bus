"""iOS Live Activity for the active Arriva bus journey."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from time import monotonic
from typing import Any

from homeassistant.components.notify import DOMAIN as NOTIFY_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from .colors import color_hex
from .const import CONF_MOBILE_DEVICE, DOMAIN, LIVE_ACTIVITY_TAG
from .coordinator import ArrivaCoordinator
from .models import BusSnapshot

_LOGGER = logging.getLogger(__name__)

_DEFAULT_COLOR = "#9E9E9E"
_ON_TIME_COLOR = "#FFFFFF"
_DELAY_COLOR = "#F44336"
_EARLY_COLOR = "#4CAF50"
_MOBILE_APP_DOMAIN = "mobile_app"
_MOBILE_DEVICE_NAME = "device_name"
_DELAY_UPDATE_INTERVAL = 60.0
_SEND_TIMEOUT = 30.0


def _format_duration(seconds: int) -> str:
    """Format a positive duration as natural Dutch text."""
    minutes, remaining_seconds = divmod(seconds, 60)
    if minutes and remaining_seconds:
        return f"{minutes} min {remaining_seconds} sec"
    if minutes:
        return f"{minutes} min"
    return f"{remaining_seconds} sec"


def _language(hass: HomeAssistant) -> str:
    """Use HA's configured language; mobile_app does not expose device locale."""
    language = getattr(getattr(hass, "config", None), "language", None)
    return "nl" if isinstance(language, str) and language.casefold().startswith("nl") else "en"


def _critical_delay_text(
    delay_seconds: int | None, language: str = "nl", *, tolerance: bool = True
) -> str:
    """Build compact Dynamic Island text."""
    if delay_seconds is None:
        return "geen info" if language == "nl" else "no info"
    if tolerance and -30 < delay_seconds <= 60:
        return "op tijd" if language == "nl" else "on time"
    value = abs(delay_seconds)
    minutes, seconds = divmod(value, 60)
    amount = f"{minutes}:{seconds:02d}" if minutes else f"{seconds}s"
    return f"{'−' if delay_seconds < 0 else '+'}{amount}"


def _activity_color(
    delay_seconds: int | None,
    delay_color: str = _DELAY_COLOR,
    early_color: str = _EARLY_COLOR,
    on_time_color: str = _ON_TIME_COLOR,
) -> str:
    """Use the dashboard delay color rules."""
    if delay_seconds is not None and delay_seconds > 60:
        return delay_color
    if delay_seconds is not None and delay_seconds <= -30:
        return early_color
    return on_time_color if delay_seconds is not None else _DEFAULT_COLOR


def _activity_icon(data: BusSnapshot) -> str:
    """Choose an icon that communicates the current journey state."""
    if data.last_journey_cancelled:
        return "mdi:cancel"
    if data.realtime_stale:
        return "mdi:cloud-alert"
    if data.is_loading:
        return "mdi:progress-clock"
    if data.target_has_passed:
        return "mdi:bus-stop"
    if not data.is_underway:
        return "mdi:clock-outline"
    if data.scheduled_wait_until is not None or data.current_stop:
        return "mdi:bus-stop"
    if data.delay_seconds is not None and data.delay_seconds > 60:
        return "mdi:bus-alert"
    if data.delay_seconds is not None and data.delay_seconds <= -30:
        return "mdi:fast-forward"
    return "mdi:bus"


def _title(data: BusSnapshot, language: str = "nl") -> str:
    """Use the scheduled journey time instead of the Companion app name."""
    prefix = (
        f"Lijn {data.line}"
        if data.line and language == "nl"
        else (f"Line {data.line}" if data.line else "Bus")
    )
    direction = "ri." if language == "nl" else "to"
    return f"{prefix} {direction} {data.destination}" if data.destination else prefix


def _message(data: BusSnapshot, language: str = "nl") -> str:
    """Use both compact body lines for the bus position."""
    scheduled = (
        dt_util.as_local(data.target_scheduled_time).strftime("%H:%M")
        if data.target_scheduled_time
        else None
    )
    if data.last_journey_cancelled:
        cancelled = (
            dt_util.as_local(data.cancelled_scheduled_time).strftime("%H:%M")
            if data.cancelled_scheduled_time
            else None
        )
        if language == "nl":
            label = f"Rit van {cancelled} uitgevallen" if cancelled else "Rit uitgevallen"
            return label + (
                f"\nVolgende: {scheduled}" if scheduled else "\nGeen volgende rit bekend"
            )
        label = f"{cancelled} service cancelled" if cancelled else "Service cancelled"
        return label + (f"\nNext: {scheduled}" if scheduled else "\nNo next service known")
    if data.target_has_passed:
        return (
            "Halte gepasseerd\nVolgende rit ophalen…"
            if language == "nl"
            else "Stop passed\nLoading next service…"
        )
    if data.is_loading and not data.realtime_stale:
        label = "Gegevens laden…" if language == "nl" else "Loading data…"
        return (f"{scheduled} · " if scheduled else "") + label
    if not data.is_underway and not data.realtime_stale:
        if scheduled:
            return (
                f"{scheduled} · {'Wacht op vertrek' if language == 'nl' else 'Waiting to depart'}"
            )
        return "Geen bus onderweg" if language == "nl" else "No bus underway"
    if data.realtime_stale and (data.current_stop or data.last_passed_stop):
        label = "Laatst bekende halte" if language == "nl" else "Last known stop"
        position = data.current_stop or data.last_passed_stop
    elif data.current_stop:
        if language == "nl":
            label = "Wacht op vertrektijd" if data.scheduled_wait_until else "Staat bij"
        else:
            label = "Waiting to depart" if data.scheduled_wait_until else "At"
        position = data.current_stop
    elif data.last_passed_stop:
        at_origin = bool(
            data.route_stop_codes and data.last_passed_stop_code == data.route_stop_codes[0]
        )
        if language == "nl":
            label = "Vertrokken vanaf" if at_origin else "Gepasseerd"
        else:
            label = "Departed from" if at_origin else "Passed"
        position = data.last_passed_stop
    else:
        label = "Locatie" if language == "nl" else "Location"
        position = "Nog niet beschikbaar" if language == "nl" else "Not available yet"
    return (f"{scheduled} · " if scheduled else "") + f"{label}: {position}"


class ArrivaLiveActivity:
    """Start, update and end one Live Activity on a selected iPhone."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: ArrivaCoordinator,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self.entry_id = entry.entry_id
        self._legacy_tag = f"{LIVE_ACTIVITY_TAG}_{entry.entry_id}"
        self._tag = f"{self._legacy_tag}_v2"
        self._settings = {**entry.data, **entry.options}
        self._coordinator = coordinator
        self._unsub: Callable[[], None] | None = None
        self._task: asyncio.Task[None] | None = None
        self._resync_requested = False
        self._active = False
        self._last_payload: tuple[object, ...] | None = None
        self._last_immediate_payload: tuple[object, ...] | None = None
        self._last_sent_at: float | None = None
        self._pending_sync: asyncio.TimerHandle | None = None
        self._stopped = False
        self._send_attempts = 0
        self._send_successes = 0
        self._last_attempt_at: str | None = None
        self._last_success_at: str | None = None
        self._last_error: str | None = None
        self._enabled = True
        # A new HA process does not know whether iOS kept an old activity.
        # Send one clear command on startup and on every runtime transition
        # that ends the activity, so a stale card can be removed remotely.
        self._clear_pending = True

    @property
    def enabled(self) -> bool:
        """Whether Live Activity updates are enabled by the user."""
        return self._enabled

    @property
    def device_count(self) -> int:
        """Number of configured mobile devices with a notify service."""
        return len(self._notification_services())

    async def async_set_enabled(self, enabled: bool) -> None:
        """Enable or disable updates and clear any stale activity immediately."""
        if self._enabled == enabled:
            return
        self._enabled = enabled
        self._cancel_pending_sync()
        if not enabled:
            if self._task is not None and not self._task.done():
                await self._task
            if await self._async_send("clear_notification", {"tag": self._tag}):
                self._active = False
                self._clear_pending = False
                self._last_payload = None
                self._last_immediate_payload = None
                self._last_sent_at = None
            else:
                self._clear_pending = True
                self._retry_sync()
        elif enabled:
            self._schedule_sync()

    @property
    def diagnostics(self) -> dict[str, Any]:
        """Report notify-action results, not proof of delivery to the iPhone."""
        return {
            "active": self._active,
            "enabled": self._enabled,
            "clear_pending": self._clear_pending,
            "configured_device_count": self.device_count,
            "pending_update": self._pending_sync is not None,
            "delay_update_interval_seconds": _DELAY_UPDATE_INTERVAL,
            "send_attempts": self._send_attempts,
            "notify_action_successes": self._send_successes,
            "last_attempt_at": self._last_attempt_at,
            "last_notify_action_success_at": self._last_success_at,
            "last_error": self._last_error,
            "device_delivery_confirmed": False,
        }

    async def async_start(self) -> None:
        """Listen for bus state changes."""
        settings = self._settings
        if CONF_MOBILE_DEVICE not in settings:
            return
        if settings.get(CONF_MOBILE_DEVICE):
            await self._async_send("clear_notification", {"tag": self._legacy_tag})
        self._unsub = self._coordinator.async_add_listener(self._schedule_sync)
        self._schedule_sync()

    async def async_update_settings(self, options: dict[str, Any]) -> None:
        """Change colours/devices in place; dismiss only removed recipients."""
        old_settings = self._settings
        old_services = set(self._notification_services())
        self._settings = {**self._entry.data, **options}
        new_services = set(self._notification_services())
        self._settings = old_settings
        if self._task is not None and not self._task.done():
            await self._task
        removed = sorted(old_services - new_services)
        if removed and not await self._async_send(
            "clear_notification", {"tag": self._tag}, services_override=removed
        ):
            raise HomeAssistantError("Unable to end activity on removed iPhone")
        self._settings = {**self._entry.data, **options}
        self._last_payload = None
        self._last_immediate_payload = None
        self._schedule_sync()

    async def async_stop(self) -> None:
        """Stop updates and dismiss the activity on every selected device."""
        self._stopped = True
        self._cancel_pending_sync()
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        task = self._task
        if task is not None:
            await task
        await self._async_send("clear_notification", {"tag": self._tag})
        self._active = False
        self._last_payload = None

    def _schedule_sync(self) -> None:
        """Coalesce coordinator bursts into one current-state update."""
        if self._stopped:
            return
        if self._task is not None and not self._task.done():
            self._resync_requested = True
            return
        self._task = self._hass.async_create_task(
            self._async_sync_loop(),
            "arriva_bus_live_activity",
        )

    def _cancel_pending_sync(self) -> None:
        if self._pending_sync is not None:
            self._pending_sync.cancel()
            self._pending_sync = None

    def _retry_sync(self) -> None:
        """Retry transient delivery errors only when a phone was configured."""
        settings = self._settings
        if settings.get(CONF_MOBILE_DEVICE):
            self._defer_sync(60)

    def _defer_sync(self, delay: float) -> None:
        """Keep one fixed deadline; never postpone it for another bus event."""
        if self._pending_sync is None and not self._stopped:
            self._pending_sync = self._hass.loop.call_later(delay, self._flush_pending_sync)

    def _flush_pending_sync(self) -> None:
        """Read the latest coordinator snapshot, not the earlier queued value."""
        self._pending_sync = None
        self._schedule_sync()

    async def _async_sync_loop(self) -> None:
        while True:
            self._resync_requested = False
            await self._async_sync()
            if not self._resync_requested:
                return

    async def _async_sync(self) -> None:
        if self._stopped:
            return
        if CONF_MOBILE_DEVICE in self._settings and not self._settings[CONF_MOBILE_DEVICE]:
            self._cancel_pending_sync()
            self._active = False
            return
        data = self._coordinator.data
        should_show = self._enabled and data.runtime_active

        if not should_show:
            self._cancel_pending_sync()
            if (self._active or self._clear_pending) and await self._async_send(
                "clear_notification",
                {"tag": self._tag},
            ):
                self._active = False
                self._clear_pending = False
                self._last_payload = None
                self._last_immediate_payload = None
                self._last_sent_at = None
            elif self._active or self._clear_pending:
                self._retry_sync()
            return

        payload = self._payload(data)
        fingerprint = (
            data.journey_key,
            payload["title"],
            payload["message"],
            payload["data"]["critical_text"],
            payload["data"]["notification_icon_color"],
            payload["data"]["notification_icon"],
            payload["data"].get("progress"),
        )
        if self._active and fingerprint == self._last_payload:
            self._cancel_pending_sync()
            return

        immediate_payload = (
            data.journey_key,
            data.operating_day,
            data.journey_number,
            payload["title"],
            payload["message"],
            data.realtime_stale,
            data.delay_seconds is None,
        )
        if (
            self._active
            and immediate_payload == self._last_immediate_payload
            and self._last_sent_at is not None
        ):
            remaining = _DELAY_UPDATE_INTERVAL - (monotonic() - self._last_sent_at)
            if remaining > 0:
                self._defer_sync(remaining)
                return

        # Position, freshness and lifecycle changes bypass the delay cooldown.
        self._cancel_pending_sync()

        if await self._async_send(
            str(payload["message"]),
            payload["data"],
            title=str(payload["title"]),
        ):
            self._active = True
            self._clear_pending = False
            self._last_payload = fingerprint
            self._last_immediate_payload = immediate_payload
            self._last_sent_at = monotonic()
        else:
            self._retry_sync()

    def _payload(self, data: BusSnapshot) -> dict[str, Any]:
        settings = self._settings
        language = _language(self._hass)

        color = _activity_color(
            None if data.realtime_stale else data.delay_seconds,
            color_hex(settings.get("delay_color"), _DELAY_COLOR),
            color_hex(settings.get("early_color"), _EARLY_COLOR),
            color_hex(settings.get("on_time_color"), _ON_TIME_COLOR),
        )
        if data.last_journey_cancelled:
            color = "#F44336"
        elif (
            data.is_loading
            or not data.is_underway
            or data.target_has_passed
            or data.realtime_stale
            or data.scheduled_wait_until is not None
        ):
            color = _DEFAULT_COLOR
        return {
            # iOS titles are immutable: keep the route header across journeys.
            "title": _title(data, language),
            "message": _message(data, language),
            "data": {
                "tag": self._tag,
                "live_update": True,
                # Prevent repeated Dynamic Island alerts. Always include this,
                # including after reload when an activity may still exist.
                # iOS may defer these lower-priority updates.
                "silent": True,
                "critical_text": (
                    ("uitval" if language == "nl" else "cancelled")
                    if data.last_journey_cancelled
                    else ("geen info" if language == "nl" else "no info")
                    if data.realtime_stale
                    else ("laden" if language == "nl" else "loading")
                    if data.is_loading
                    else _critical_delay_text(
                        data.delay_seconds, language, tolerance=data.is_underway
                    )
                    if data.is_underway
                    or (
                        data.journey_number is not None
                        and data.delay_seconds is not None
                        and data.delay_seconds > 0
                    )
                    else ("wacht" if language == "nl" else "waiting")
                ),
                "notification_icon": _activity_icon(data),
                "notification_icon_color": color,
                "progress_bar_color": color,
                **(
                    {
                        "progress": data.route_progress if data.is_underway else 0,
                        "progress_max": 100,
                        "progress_bar_direction": "increasing",
                    }
                    if data.route_progress is not None
                    and data.is_underway
                    and not data.is_loading
                    and not data.last_journey_cancelled
                    and not data.realtime_stale
                    and data.scheduled_wait_until is None
                    else {}
                ),
                "url": self._device_url(),
                # Let the Companion app choose its default background/text.
                # Transparent backgrounds are not a supported payload option.
            },
        }

    def _device_url(self) -> str:
        """Open this configured route's device and its entities in HA."""
        device = dr.async_get(self._hass).async_get_device(identifiers={(DOMAIN, self.entry_id)})
        if device is not None:
            return f"/config/devices/device/{device.id}"
        return f"/config/integrations/integration/{DOMAIN}"

    def _notification_services(self) -> list[str]:
        settings = self._settings
        selected = settings.get(CONF_MOBILE_DEVICE, [])
        device_ids = [selected] if isinstance(selected, str) else selected
        if not isinstance(device_ids, list):
            return []
        services: list[str] = []
        registry = dr.async_get(self._hass)
        for device_id in device_ids:
            if not isinstance(device_id, str):
                continue
            device = registry.async_get(device_id)
            if device is None:
                continue
            for entry_id in device.config_entries:
                mobile_entry = self._hass.config_entries.async_get_entry(entry_id)
                if mobile_entry is None or mobile_entry.domain != _MOBILE_APP_DOMAIN:
                    continue
                device_name = mobile_entry.data.get(_MOBILE_DEVICE_NAME)
                if isinstance(device_name, str):
                    service = slugify(f"mobile_app_{device_name}")
                    if service not in services:
                        services.append(service)
        return services

    def _notification_service(self) -> str | None:
        """Return the first service for backwards-compatible diagnostics/tests."""
        return next(iter(self._notification_services()), None)

    async def _async_send(
        self,
        message: str,
        data: dict[str, Any],
        *,
        title: str | None = None,
        services_override: list[str] | None = None,
    ) -> bool:
        self._send_attempts += 1
        self._last_attempt_at = dt_util.utcnow().isoformat()
        # Keep the old single-service hook usable for existing diagnostics
        # tooling while normal operation fans out to every selected device.
        legacy_selector = self.__dict__.get("_notification_service")
        if legacy_selector is not None:
            selected_service = legacy_selector()
            candidate_services = [selected_service] if isinstance(selected_service, str) else []
        else:
            candidate_services = self._notification_services()
        if services_override is not None:
            candidate_services = services_override
        services = [
            service
            for service in candidate_services
            if self._hass.services.has_service(NOTIFY_DOMAIN, service)
        ]
        if not services:
            self._last_error = "notification_action_unavailable"
            _LOGGER.warning("Selected iPhone notification action is unavailable")
            return False
        service_data: dict[str, Any] = {"message": message, "data": data}
        if title is not None:
            service_data["title"] = title
        succeeded = len(services) == len(candidate_services)
        for service in services:
            try:
                async with asyncio.timeout(_SEND_TIMEOUT):
                    await self._hass.services.async_call(
                        NOTIFY_DOMAIN, service, service_data, blocking=True
                    )
            except TimeoutError:
                succeeded = False
                self._last_error = "notification_timeout"
                _LOGGER.warning("Timed out updating Arriva bus Live Activity")
            except HomeAssistantError:
                succeeded = False
                self._last_error = "notification_send_failed"
                _LOGGER.warning("Unable to update Arriva bus Live Activity", exc_info=True)
            else:
                self._send_successes += 1
                self._last_success_at = dt_util.utcnow().isoformat()
        if succeeded:
            self._last_error = None
        return succeeded
