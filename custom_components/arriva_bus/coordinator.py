"""Coordinator for Arriva Bus (test)."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import replace
from datetime import date, datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.event import (
    async_track_time_interval,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .api import TransitHttpClient, TransitHttpError
from .const import (
    DATA_OWNER,
    INVALID_JOURNEY_REMEMBER,
    JOURNEY_DISCOVERY_INTERVAL,
    JOURNEY_REVALIDATE_INTERVAL,
    JOURNEY_WITHOUT_TIME_MAX_AGE,
    MAINTENANCE_INTERVAL,
    NAME,
    NO_SHOW_GRACE_AFTER_TARGET,
    PASSED_JOURNEY_REMEMBER,
    PSA_RETRY_INTERVAL,
    REALTIME_STALE_AFTER,
    RECENT_KV6_EVENTS,
    SOURCE_FUTURE_TOLERANCE,
    TRUSTED_KV6_SOURCE,
)
from .models import BusSnapshot, JourneySelection, Kv6Event
from .realtime import Kv6Subscriber
from .route_config import RouteConfig
from .runtime import (
    TRIP_START_EVENTS,
    should_reject_implausibly_early,
)

_LOGGER = logging.getLogger(__name__)

_UNDERWAY_EVENTS = frozenset({"ONROUTE", "ARRIVAL", "ONSTOP", "DEPARTURE"})
_STOP_PROGRESS_EVENTS = frozenset({"DEPARTURE", "ONROUTE"})


class ArrivaCoordinator(DataUpdateCoordinator[BusSnapshot]):
    """Combine a gated runtime, journey selection and raw Arriva KV6."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: TransitHttpClient,
    ) -> None:
        self._entry = entry
        self.entry_id = entry.entry_id
        self.route = RouteConfig.from_data(entry.data)
        self._client = client
        self._kv6: Kv6Subscriber | None = None
        self._kv6_task: asyncio.Task[None] | None = None
        self._bootstrap_task: asyncio.Task[None] | None = None
        self._maintenance_task: asyncio.Task[None] | None = None
        self._maintenance_unsub: CALLBACK_TYPE | None = None
        self._runtime_lock = asyncio.Lock()
        self._maintenance_lock = asyncio.Lock()
        self._runtime_active = False
        self.enabled = False
        self._inactive_reason = "manually_disabled"
        self._shutdown = False

        self._active: JourneySelection | None = None
        self._active_selected_at: datetime | None = None
        self._next_journey_refresh_at: datetime | None = None
        self._recent_events: deque[Kv6Event] = deque(maxlen=RECENT_KV6_EVENTS)
        self._passed_journeys: dict[str, datetime] = {}
        self._invalid_journeys: dict[str, datetime] = {}
        self._selection_error: str | None = None
        self._last_drgl_success_at: datetime | None = None

        # Official BISON PassengerStopAssignment normalization table:
        # ARR UserStopCode -> national CHB StopPlaceCode.
        self._stop_place_by_user_stop: dict[str, str] = {}
        self._psa_retry_after: datetime | None = None

        # PassageSequenceNumber is per UserStopCode, not a global route order.
        self._last_event_order_timestamp: datetime | None = None
        self._last_stop_progress_timestamp: datetime | None = None
        self._stop_name_tasks: dict[str, asyncio.Task[None]] = {}

        super().__init__(
            hass,
            _LOGGER,
            name=NAME,
            config_entry=entry,
            update_interval=None,
            always_update=False,
        )

        self.data = self._idle_snapshot(inactive_reason=self._inactive_reason)

    @property
    def timing_point_code(self) -> str:
        """Return the national target StopPlaceCode."""
        return self.route.target_stop_code

    @property
    def line_id(self) -> str:
        """Return the NDOV line identifier."""
        return f"{DATA_OWNER}:{self.route.line_planning_number}"

    @property
    def runtime_active(self) -> bool:
        """Return whether network and parsing work is currently allowed."""
        return self._runtime_active

    @property
    def inactive_reason(self) -> str | None:
        """Return why the runtime is sleeping."""
        return self._inactive_reason

    @property
    def realtime_connected(self) -> bool:
        """Return whether the KV6 stream has delivered a recent frame."""
        return self._kv6.connected if self._kv6 is not None else False

    @property
    def last_frame_received_at(self) -> datetime | None:
        """Return local receipt time of the latest Arriva frame."""
        return self._kv6.last_frame_received_at if self._kv6 is not None else None

    @property
    def selection_error(self) -> str | None:
        """Return the latest optional DRGL selection error."""
        return self._selection_error

    @property
    def active_selected_at(self) -> datetime | None:
        """Return when the current candidate was selected."""
        return self._active_selected_at

    @property
    def last_drgl_success_at(self) -> datetime | None:
        """Return the latest successful journey-list refresh."""
        return self._last_drgl_success_at

    @property
    def psa_loaded_for(self) -> date | None:
        """Return the date represented by the active stop mapping."""
        return self._client.psa_loaded_for

    @property
    def psa_is_stale(self) -> bool:
        """Return whether the retained stop mapping is stale."""
        return self._client.psa_is_stale

    @property
    def stop_mapping_count(self) -> int:
        """Return the number of active operator-to-national stop mappings."""
        return len(self._stop_place_by_user_stop)

    async def async_start(self) -> None:
        """Remain idle until the runtime switch restores or receives an on command."""
        self.async_set_updated_data(self.data)

    async def async_shutdown(self) -> None:
        """Stop listeners, network work and coordinator timers."""
        if self._shutdown:
            return
        self._shutdown = True
        await self._async_deactivate("integration_unloaded")
        await super().async_shutdown()

    def _runtime_reason(self, now: datetime | None = None) -> str | None:
        return None if self.enabled else "manually_disabled"

    async def async_set_enabled(self, enabled: bool) -> None:
        """Apply the runtime switch without presence or calendar gates."""
        self.enabled = enabled
        await self._async_evaluate_runtime()
        self.async_set_updated_data(self.data)

    async def _async_evaluate_runtime(self, now: datetime | None = None) -> None:
        if self._shutdown:
            return
        async with self._runtime_lock:
            reason = self._runtime_reason(now)
            if reason is None and not self._runtime_active:
                await self._async_activate()
                return
            if reason is not None and self._runtime_active:
                await self._async_deactivate(reason)
                return
            if reason != self._inactive_reason:
                self._inactive_reason = reason
                if not self._runtime_active:
                    self.async_set_updated_data(self._idle_snapshot(inactive_reason=reason))

    async def _async_activate(self) -> None:
        """Open the runtime and start its only background stream."""
        _LOGGER.info("Activating Arriva bus runtime")
        self._runtime_active = True
        self._inactive_reason = None
        self._active = None
        self._active_selected_at = None
        self._next_journey_refresh_at = None
        self._recent_events.clear()
        self._last_event_order_timestamp = None
        self._last_stop_progress_timestamp = None
        self.async_set_updated_data(replace(self._idle_snapshot(), is_loading=True))

        self._kv6 = Kv6Subscriber(self.route.line_planning_number)
        self._kv6_task = self._entry.async_create_background_task(
            self.hass,
            self._kv6.async_run(self._async_handle_kv6_event),
            "arriva_bus_kv6",
        )
        self._maintenance_unsub = async_track_time_interval(
            self.hass,
            self._async_maintenance_tick,
            MAINTENANCE_INTERVAL,
            name="arriva_bus",
            cancel_on_shutdown=True,
        )
        self._bootstrap_task = self._entry.async_create_task(
            self.hass,
            self._async_bootstrap(),
            "arriva_bus_bootstrap",
        )
        self._bootstrap_task.add_done_callback(self._bootstrap_done)

    async def _async_bootstrap(self) -> None:
        """Load active-morning data without blocking a later gate event."""
        await self._async_refresh_psa(silent=True)
        await self._async_maintenance(force_journey_refresh=True)

    def _bootstrap_done(self, task: asyncio.Task[None]) -> None:
        if self._bootstrap_task is task:
            self._bootstrap_task = None

    async def _async_deactivate(self, reason: str) -> None:
        """Close every active resource so sleeping means no transport work."""
        was_active = self._runtime_active
        self._runtime_active = False
        self._inactive_reason = reason

        if self._maintenance_unsub is not None:
            self._maintenance_unsub()
            self._maintenance_unsub = None

        if self._bootstrap_task is not None:
            self._bootstrap_task.cancel()
            await asyncio.gather(self._bootstrap_task, return_exceptions=True)
            self._bootstrap_task = None

        if self._maintenance_task is not None:
            self._maintenance_task.cancel()
            await asyncio.gather(self._maintenance_task, return_exceptions=True)
            self._maintenance_task = None

        tasks = tuple(self._stop_name_tasks.values())
        self._stop_name_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        if self._kv6 is not None:
            await self._kv6.async_stop()
        if self._kv6_task is not None:
            self._kv6_task.cancel()
            try:
                await self._kv6_task
            except asyncio.CancelledError:
                pass
        self._kv6_task = None
        self._kv6 = None

        self._active = None
        self._active_selected_at = None
        self._next_journey_refresh_at = None
        self._recent_events.clear()
        self._passed_journeys.clear()
        self._invalid_journeys.clear()
        self._last_event_order_timestamp = None
        self._last_stop_progress_timestamp = None
        self.async_set_updated_data(self._idle_snapshot(inactive_reason=reason))
        if was_active:
            _LOGGER.info("Arriva bus runtime sleeping (%s)", reason)

    async def _async_update_data(self) -> BusSnapshot:
        """Support a manual coordinator refresh without enabling polling."""
        await self._async_maintenance(force_journey_refresh=True)
        return self.data

    async def _async_maintenance_tick(self, _: datetime) -> None:
        await self._async_maintenance()

    async def _async_maintenance(self, *, force_journey_refresh: bool = False) -> None:
        """Perform bounded maintenance only while the morning gate is open."""
        if not self._runtime_active:
            return

        async with self._maintenance_lock:
            if not self._runtime_active:
                return
            now = dt_util.now()
            self._expire_journey_filters(now)

            psa_due = self._client.psa_loaded_for != now.date()
            retry_due = self._psa_retry_after is not None and now >= self._psa_retry_after
            if psa_due and (self._psa_retry_after is None or retry_due):
                await self._async_refresh_psa()

            if self.data.journey_key is not None:
                pending_stop_codes = {
                    code
                    for code in (
                        self.data.current_stop_code,
                        self.data.last_passed_stop_code,
                    )
                    if code is not None
                }
                for pending_stop_code in pending_stop_codes:
                    if self._client.stop_name_resolution_due(pending_stop_code):
                        self._schedule_stop_name_resolution(
                            pending_stop_code,
                            self.data.journey_key,
                        )

            trip_data_stale = (
                self.data.is_underway
                and self.data.last_received_at is not None
                and (now - self.data.last_received_at) > REALTIME_STALE_AFTER
            )
            stream_reference = self.last_frame_received_at or self._active_selected_at
            stream_stale = (
                self._active is not None
                and not self.realtime_connected
                and stream_reference is not None
                and (now - stream_reference) > REALTIME_STALE_AFTER
            )
            initial_data_timed_out = (
                self.data.is_loading
                and self._active_selected_at is not None
                and now - self._active_selected_at > REALTIME_STALE_AFTER
            )
            if not self.data.realtime_stale and (
                trip_data_stale or stream_stale or initial_data_timed_out
            ):
                _LOGGER.warning("Arriva bus realtime state became stale")
                self.async_set_updated_data(
                    replace(
                        self.data,
                        delay_seconds=None,
                        scheduled_wait_until=None,
                        delay_source_stop=None,
                        delay_source_status=None,
                        delay_observed_at=None,
                        realtime_connected=self.realtime_connected,
                        realtime_stale=True,
                        is_loading=False,
                    )
                )

            if self._active is not None and not self.data.is_underway:
                expired = False
                scheduled = self._active.scheduled_target
                if scheduled is not None:
                    expired = now > scheduled + NO_SHOW_GRACE_AFTER_TARGET
                elif self._active_selected_at is not None:
                    expired = now > self._active_selected_at + JOURNEY_WITHOUT_TIME_MAX_AGE
                if expired:
                    _LOGGER.warning("Dropping Arriva bus no-show journey %s", self._active.key)
                    self._invalid_journeys[self._active.key] = now
                    self._clear_active_journey()
                    self.async_set_updated_data(
                        self._idle_snapshot(
                            last_journey_cancelled=self.data.last_journey_cancelled,
                            cancelled_scheduled_time=self.data.cancelled_scheduled_time,
                            preserve_position=True,
                        )
                    )
                    force_journey_refresh = True

            refresh_due = (
                self._next_journey_refresh_at is None or now >= self._next_journey_refresh_at
            )
            if force_journey_refresh or (not self.data.is_underway and refresh_due):
                await self._async_refresh_journeys(now)

            snapshot = replace(
                self.data,
                runtime_active=True,
                inactive_reason=None,
                realtime_connected=self.realtime_connected,
            )
            if snapshot != self.data:
                self.async_set_updated_data(snapshot)

    async def _async_refresh_psa(self, *, silent: bool = False) -> None:
        """Refresh stop normalization while retaining a stale working map."""
        try:
            mapping = await self._client.async_get_passenger_stop_assignments()
        except TransitHttpError as err:
            self._psa_retry_after = dt_util.now() + PSA_RETRY_INTERVAL
            log = _LOGGER.debug if silent else _LOGGER.warning
            log("PassengerStopAssignment refresh failed: %s", err)
            return

        self._stop_place_by_user_stop = mapping
        if self._client.psa_is_stale:
            self._psa_retry_after = dt_util.now() + PSA_RETRY_INTERVAL
            _LOGGER.warning(
                "Using stale PassengerStopAssignment %s; retrying later",
                self._client.psa_filename or "NDOV",
            )
        else:
            self._psa_retry_after = None

        target_user_codes = sum(
            1 for stop_place in mapping.values() if stop_place == self.route.target_stop_code
        )
        _LOGGER.info(
            "Loaded %d active Arriva PassengerStopAssignments from %s; "
            "%d UserStopCode(s) map to target stop",
            len(mapping),
            self._client.psa_filename or "NDOV",
            target_user_codes,
        )

    async def _async_refresh_journeys(self, now: datetime) -> None:
        """Select or revalidate one journey with adaptive HTTP frequency."""
        try:
            candidates = await self._client.async_get_journeys()
        except TransitHttpError as err:
            if self.data.is_loading and self._active is None:
                self.async_set_updated_data(
                    replace(self.data, is_loading=False, realtime_stale=True)
                )
            self._selection_error = str(err)
            self._next_journey_refresh_at = now + JOURNEY_DISCOVERY_INTERVAL
            _LOGGER.warning("Arriva bus journey refresh failed: %s", err)
            return

        self._selection_error = None
        self._last_drgl_success_at = now
        self._expire_journey_filters(now)
        cancellation_detected = self.data.last_journey_cancelled
        cancelled_scheduled_time = self.data.cancelled_scheduled_time

        if self._active is not None:
            refreshed = next(
                (candidate for candidate in candidates if candidate.key == self._active.key),
                None,
            )
            if refreshed is None:
                # Missing departure data is not a cancellation. Retain the
                # selection so subsequent vehicle events can recover it; the
                # existing no-show deadline still bounds the waiting period.
                _LOGGER.info(
                    "Selected Arriva bus journey %s disappeared from the departure list; "
                    "retaining selection without declaring cancellation",
                    self._active.key,
                )
                self._next_journey_refresh_at = now + JOURNEY_REVALIDATE_INTERVAL
                return
            if refreshed is not None and not refreshed.cancelled:
                self._active = refreshed
                if self.data.target_scheduled_time != refreshed.scheduled_target:
                    self.async_set_updated_data(
                        replace(
                            self.data,
                            target_scheduled_time=refreshed.scheduled_target,
                        )
                    )
                self._next_journey_refresh_at = now + JOURNEY_REVALIDATE_INTERVAL
                return

            cancellation_detected = True
            cancelled_scheduled_time = self._active.scheduled_target or cancelled_scheduled_time
            _LOGGER.info("Selected Arriva bus journey %s was cancelled", self._active.key)
            self._invalid_journeys[self._active.key] = now
            self._clear_active_journey()

        chosen: JourneySelection | None = None
        for candidate in candidates:
            if candidate.key in self._passed_journeys or candidate.key in self._invalid_journeys:
                continue
            if candidate.cancelled:
                cancellation_detected = True
                cancelled_scheduled_time = candidate.scheduled_target or cancelled_scheduled_time
                self._invalid_journeys[candidate.key] = now
                continue
            chosen = candidate
            break

        if chosen is None:
            self._next_journey_refresh_at = now + JOURNEY_DISCOVERY_INTERVAL
            self.async_set_updated_data(
                self._idle_snapshot(
                    last_journey_cancelled=cancellation_detected,
                    cancelled_scheduled_time=cancelled_scheduled_time,
                    preserve_position=cancellation_detected,
                )
            )
            return

        try:
            route = await self._client.async_get_route(chosen)
            dwells = self._client.get_dwell_calls(chosen)
        except TransitHttpError:
            _LOGGER.warning("Route unavailable; omitting route progress for %s", chosen.key)
            route = ()
            dwells = ()
        if not self._runtime_active or self._shutdown:
            return
        self._active = chosen
        self._active_selected_at = now
        self._last_event_order_timestamp = None
        self._last_stop_progress_timestamp = None
        self._next_journey_refresh_at = now + JOURNEY_REVALIDATE_INTERVAL
        self.async_set_updated_data(
            BusSnapshot(
                is_loading=True,
                route_stop_codes=route,
                route_dwell_calls=dwells,
                journey_number=chosen.journey_number,
                operating_day=chosen.operating_day,
                journey_key=chosen.key,
                line=self.route.line_public_number,
                destination=self.route.destination,
                runtime_active=True,
                target_scheduled_time=chosen.scheduled_target,
                realtime_connected=self.realtime_connected,
                last_journey_cancelled=cancellation_detected,
                cancelled_scheduled_time=(
                    cancelled_scheduled_time if cancellation_detected else None
                ),
            )
        )
        _LOGGER.info("Tracking Arriva bus journey %s", chosen.key)

        for event in tuple(self._recent_events):
            if not self._event_matches_active(event):
                continue
            if self._is_implausibly_early(event):
                await self._async_reject_active_journey(event)
                return
            await self._async_apply_event(event)
            if self._active is None:
                return

    async def _async_handle_kv6_event(self, event: Kv6Event) -> None:
        """Retain only trustworthy physical events for the selected bus."""
        if not self._runtime_active:
            return
        if event.data_owner_code != DATA_OWNER:
            return
        if event.line_planning_number != self.route.line_planning_number:
            return
        if event.reinforcement_number not in (None, 0):
            return
        if not self._is_trusted_vehicle_event(event):
            return

        self._recent_events.append(event)
        if not self._event_matches_active(event):
            return

        if self._is_implausibly_early(event):
            await self._async_reject_active_journey(event)
            return
        await self._async_apply_event(event)

    @staticmethod
    def _is_trusted_vehicle_event(event: Kv6Event) -> bool:
        source = (event.source or "").upper()
        vehicle = (event.vehicle_number or "").strip()
        return source == TRUSTED_KV6_SOURCE and vehicle not in {"", "0"}

    def _event_matches_active(self, event: Kv6Event) -> bool:
        active = self._active
        if active is None:
            return False
        return (
            event.journey_number == active.journey_number
            and event.operating_day == active.operating_day
            and event.reinforcement_number in (None, 0)
        )

    def _scheduled_wait(self, event: Kv6Event) -> datetime | None:
        if event.event_type not in {"ARRIVAL", "ONSTOP"} or event.timestamp is None:
            return None
        if event.punctuality is None or event.punctuality >= 0:
            return None
        code = self._stop_place_by_user_stop.get(event.user_stop_code)
        for call in self.data.route_dwell_calls:
            # Only an explicit stop-specific timetable interval proves waiting.
            # Keep the implausibly-early guard for vehicles far before arrival.
            if (
                call.stop_code == code
                and call.arrival - timedelta(minutes=10) <= event.timestamp <= call.departure
            ):
                return call.departure
        return None

    def _is_implausibly_early(self, event: Kv6Event) -> bool:
        if event.event_type == "END" or self._scheduled_wait(event) is not None:
            return False
        return should_reject_implausibly_early(
            event.event_type,
            event.punctuality,
            trip_underway=self.data.is_underway,
        )

    def _event_order_time(self, event: Kv6Event) -> datetime | None:
        """Use source time unless its vehicle clock is implausibly ahead."""
        if event.timestamp is None:
            return event.received_at
        if (
            event.received_at is not None
            and event.timestamp > event.received_at + SOURCE_FUTURE_TOLERANCE
        ):
            return event.received_at
        return event.timestamp

    async def _async_reject_active_journey(self, event: Kv6Event) -> None:
        active = self._active
        if active is None:
            return
        _LOGGER.warning(
            "Ignoring Arriva bus journey %s: trusted vehicle %s reported "
            "implausible punctuality %ss from %s",
            active.key,
            event.vehicle_number,
            event.punctuality,
            event.event_type,
        )
        self._invalid_journeys[active.key] = dt_util.now()
        self._clear_active_journey()
        self.async_set_updated_data(self._idle_snapshot())
        self._request_maintenance(force_journey_refresh=True)

    async def _async_finish_active_journey(self, reason: str) -> None:
        active = self._active
        if active is None:
            return
        ended_before_target = reason == "END" and not self.data.target_has_passed
        if ended_before_target:
            _LOGGER.warning(
                "Arriva bus journey %s received END before confirmed target stop passage; "
                "retaining selection pending vehicle data or explicit cancellation",
                active.key,
            )
            self.async_set_updated_data(
                replace(
                    self.data,
                    is_underway=False,
                    realtime_stale=True,
                    delay_seconds=None,
                    delay_source_stop=None,
                    delay_source_status=None,
                    delay_observed_at=None,
                )
            )
            self._request_maintenance(force_journey_refresh=True)
            return
        _LOGGER.info("Arriva bus journey %s finished (%s)", active.key, reason)
        self._passed_journeys[active.key] = dt_util.now()
        self._clear_active_journey()
        self.async_set_updated_data(self._idle_snapshot())
        self._request_maintenance(force_journey_refresh=True)

    async def _async_apply_event(self, event: Kv6Event) -> None:
        """Apply one trusted physical-vehicle measurement without HTTP I/O."""
        active = self._active
        if active is None or not self._runtime_active:
            return
        current = self.data
        if current.journey_number != active.journey_number:
            return
        if event.event_type not in _UNDERWAY_EVENTS and event.event_type != "END":
            return

        order_time = self._event_order_time(event)
        if event.event_type == "END" and order_time is None:
            _LOGGER.debug("Ignoring undated END for Arriva bus journey %s", active.key)
            return
        if (
            order_time is not None
            and self._last_event_order_timestamp is not None
            and order_time < self._last_event_order_timestamp
        ):
            return

        self._last_event_order_timestamp = order_time or self._last_event_order_timestamp
        if event.event_type == "END":
            await self._async_finish_active_journey("END")
            return
        trip_started = current.is_underway or event.event_type in TRIP_START_EVENTS
        updated = replace(
            current,
            runtime_active=True,
            inactive_reason=None,
            is_loading=False,
            is_underway=trip_started,
            delay_seconds=current.delay_seconds if trip_started else None,
            scheduled_wait_until=None,
            realtime_connected=True,
            realtime_stale=False,
            data_timestamp=event.timestamp or current.data_timestamp,
            last_received_at=event.received_at or dt_util.now(),
            last_event_type=event.event_type,
            vehicle_number=event.vehicle_number,
            last_journey_cancelled=(False if trip_started else current.last_journey_cancelled),
            cancelled_scheduled_time=(None if trip_started else current.cancelled_scheduled_time),
        )

        stop_place_code = None
        if event.user_stop_code:
            stop_place_code = self._stop_place_by_user_stop.get(event.user_stop_code)
        cached_stop_name = (
            self._client.get_cached_stop_name(stop_place_code)
            if stop_place_code is not None
            else None
        )

        current_stop_name = (
            current.current_stop if current.current_stop_code == stop_place_code else None
        )
        last_passed_stop_name = (
            current.last_passed_stop if current.last_passed_stop_code == stop_place_code else None
        )
        event_stop_name = cached_stop_name or current_stop_name or last_passed_stop_name

        # Retain reported positive delay before departure without marking the
        # journey underway. Planned dwell time must not look like a late start.
        waiting_delay = event.punctuality
        if not trip_started and event.timestamp is not None:
            for call in current.route_dwell_calls:
                if call.stop_code == stop_place_code:
                    waiting_delay = min(
                        event.punctuality or 0,
                        max(0, int((event.timestamp - call.departure).total_seconds())),
                    )
                    break
        if event.punctuality is not None and (
            trip_started or (waiting_delay is not None and waiting_delay > 0)
        ):
            wait_until = self._scheduled_wait(event)
            updated = replace(
                updated,
                delay_seconds=(
                    0
                    if wait_until is not None
                    else event.punctuality
                    if trip_started
                    else waiting_delay
                ),
                raw_delay_seconds=event.punctuality,
                scheduled_wait_until=wait_until,
                delay_source_stop=event_stop_name or current.last_passed_stop,
                delay_source_status=event.event_type,
                delay_observed_at=event.timestamp,
            )

        # Use verified stop codes, never guessed name matches or elapsed time.
        if stop_place_code in current.route_stop_codes and len(current.route_stop_codes) > 1:
            index = current.route_stop_codes.index(stop_place_code)
            progress = round(100 * index / (len(current.route_stop_codes) - 1))
            updated = replace(updated, route_progress=max(current.route_progress or 0, progress))

        stop_progress_event = event.event_type in _STOP_PROGRESS_EVENTS
        at_stop_event = event.event_type in {"ARRIVAL", "ONSTOP"}
        if at_stop_event and event.user_stop_code:
            if not stop_place_code:
                _LOGGER.debug(
                    "No active PassengerStopAssignment for ARR UserStopCode %s",
                    event.user_stop_code,
                )
                updated = replace(updated, current_stop=None, current_stop_code=None)
            else:
                updated = replace(
                    updated,
                    current_stop=event_stop_name,
                    current_stop_code=stop_place_code,
                )
                if cached_stop_name is None:
                    self._schedule_stop_name_resolution(stop_place_code, active.key)

        if stop_progress_event:
            updated = replace(updated, current_stop=None, current_stop_code=None)

        if stop_progress_event and event.user_stop_code:
            if not stop_place_code:
                _LOGGER.debug(
                    "No active PassengerStopAssignment for ARR UserStopCode %s",
                    event.user_stop_code,
                )
            elif stop_place_code == self.route.target_stop_code:
                self._passed_journeys[active.key] = dt_util.now()
                self._clear_active_journey()
                self.async_set_updated_data(
                    replace(
                        updated,
                        is_underway=False,
                        target_has_passed=True,
                        last_passed_stop=None,
                        last_passed_stop_code=None,
                        current_stop=None,
                        current_stop_code=None,
                        last_journey_cancelled=False,
                        cancelled_scheduled_time=None,
                    )
                )
                _LOGGER.info("Arriva bus journey %s passed target stop", active.key)
                self._request_maintenance(force_journey_refresh=True)
                return
            else:
                is_newer = (
                    order_time is None
                    or self._last_stop_progress_timestamp is None
                    or order_time > self._last_stop_progress_timestamp
                )
                if is_newer:
                    updated = replace(
                        updated,
                        last_passed_stop=event_stop_name or current.last_passed_stop,
                        last_passed_stop_code=stop_place_code,
                    )
                    if order_time is not None:
                        self._last_stop_progress_timestamp = order_time
                    if cached_stop_name is None:
                        self._schedule_stop_name_resolution(stop_place_code, active.key)

        if updated != current:
            self.async_set_updated_data(updated)

    def _schedule_stop_name_resolution(self, stop_place_code: str, journey_key: str) -> None:
        if stop_place_code in self._stop_name_tasks or not self._runtime_active:
            return
        task = self._entry.async_create_task(
            self.hass,
            self._async_resolve_stop_name(stop_place_code, journey_key),
            f"arriva_bus_stop_name_{stop_place_code}",
        )
        self._stop_name_tasks[stop_place_code] = task
        task.add_done_callback(lambda _: self._stop_name_tasks.pop(stop_place_code, None))

    async def _async_resolve_stop_name(self, stop_place_code: str, journey_key: str) -> None:
        name = await self._client.async_get_stop_name(stop_place_code)
        if not name or not self._runtime_active:
            return
        current = self.data
        if current.journey_key != journey_key:
            return
        changes: dict[str, str] = {}
        if current.current_stop_code == stop_place_code:
            changes["current_stop"] = name
        if current.last_passed_stop_code == stop_place_code:
            changes["last_passed_stop"] = name
        if changes:
            self.async_set_updated_data(replace(current, **changes))

    def _clear_active_journey(self) -> None:
        self._active = None
        self._active_selected_at = None
        self._next_journey_refresh_at = None
        self._last_event_order_timestamp = None
        self._last_stop_progress_timestamp = None

    def _request_maintenance(self, *, force_journey_refresh: bool = False) -> None:
        if not self._runtime_active or self._shutdown:
            return
        if self._maintenance_task is not None:
            if force_journey_refresh:
                self._maintenance_refresh_pending = True
            return
        self._maintenance_task = self._entry.async_create_task(
            self.hass,
            self._async_maintenance(force_journey_refresh=force_journey_refresh),
            "arriva_bus_maintenance",
        )
        self._maintenance_task.add_done_callback(self._maintenance_done)

    def _maintenance_done(self, task: asyncio.Task[None]) -> None:
        if self._maintenance_task is task:
            self._maintenance_task = None
            if getattr(self, "_maintenance_refresh_pending", False):
                self._maintenance_refresh_pending = False
                self._request_maintenance(force_journey_refresh=True)

    def _idle_snapshot(
        self,
        *,
        inactive_reason: str | None = None,
        last_journey_cancelled: bool = False,
        cancelled_scheduled_time: datetime | None = None,
        preserve_position: bool = False,
    ) -> BusSnapshot:
        snapshot = BusSnapshot(
            line=self.route.line_public_number,
            destination=self.route.destination,
            runtime_active=self._runtime_active,
            inactive_reason=inactive_reason,
            realtime_connected=self.realtime_connected,
            last_journey_cancelled=last_journey_cancelled,
            cancelled_scheduled_time=(cancelled_scheduled_time if last_journey_cancelled else None),
        )
        if not preserve_position:
            return snapshot
        return replace(
            snapshot,
            current_stop=self.data.current_stop,
            current_stop_code=self.data.current_stop_code,
            last_passed_stop=self.data.last_passed_stop,
            last_passed_stop_code=self.data.last_passed_stop_code,
            data_timestamp=self.data.data_timestamp,
            last_received_at=self.data.last_received_at,
            last_event_type=self.data.last_event_type,
            vehicle_number=self.data.vehicle_number,
            realtime_stale=self.data.realtime_stale,
        )

    def _expire_journey_filters(self, now: datetime) -> None:
        passed_cutoff = now - PASSED_JOURNEY_REMEMBER
        invalid_cutoff = now - INVALID_JOURNEY_REMEMBER
        self._passed_journeys = {
            key: seen for key, seen in self._passed_journeys.items() if seen >= passed_cutoff
        }
        self._invalid_journeys = {
            key: seen for key, seen in self._invalid_journeys.items() if seen >= invalid_cutoff
        }
