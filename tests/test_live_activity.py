"""Tests for the Arriva beta iOS Live Activity."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.arriva_bus.live_activity import (
    ArrivaLiveActivity,
    _activity_color,
    _activity_icon,
    _critical_delay_text,
    _format_duration,
    _message,
    _title,
)
from custom_components.arriva_bus.models import BusSnapshot


def test_delay_formatting_matches_dashboard_rules() -> None:
    assert _format_duration(9) == "9 sec"
    assert _format_duration(121) == "2 min 1 sec"
    assert _critical_delay_text(121) == "+2:01"
    assert _critical_delay_text(70) == "+1:10"
    assert _critical_delay_text(-72) == "−1:12"
    assert _critical_delay_text(-30) == "−30s"


def test_delay_colors_match_dashboard_rules() -> None:
    assert _activity_color(60) == "#FFFFFF"
    assert _activity_color(61) == "#F44336"
    assert _activity_color(-9) == "#FFFFFF"
    assert _activity_color(-29) == "#FFFFFF"
    assert _activity_color(-30) == "#4CAF50"


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"delay_seconds": 0}, "#FFFFFF"),
        ({"is_underway": False, "delay_seconds": 121}, "#9E9E9E"),
        ({"last_journey_cancelled": True, "delay_seconds": -60}, "#F44336"),
        ({"last_journey_cancelled": True, "is_underway": False}, "#F44336"),
        ({"realtime_stale": True}, "#9E9E9E"),
        ({"target_has_passed": True}, "#9E9E9E"),
        ({"scheduled_wait_until": datetime(2026, 9, 17, 12, tzinfo=UTC)}, "#9E9E9E"),
    ],
)
def test_status_colors_override_delay_when_no_running_bus(changes, expected):
    snapshot = replace(_underway(), **changes)
    manager = _manager(snapshot)
    manager._settings = {"delay_color": "purple", "early_color": "blue"}
    payload = manager._payload(snapshot)
    assert payload["data"]["notification_icon_color"] == expected
    assert payload["data"]["progress_bar_color"] == expected


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"is_underway": False}, "mdi:clock-outline"),
        ({"delay_seconds": 0}, "mdi:bus"),
        ({"delay_seconds": 61}, "mdi:bus-alert"),
        ({"delay_seconds": -30}, "mdi:fast-forward"),
        ({"current_stop": "Gulpen"}, "mdi:bus-stop"),
        (
            {"scheduled_wait_until": datetime(2026, 9, 17, 12, tzinfo=UTC)},
            "mdi:bus-stop",
        ),
        ({"target_has_passed": True}, "mdi:bus-stop"),
        ({"realtime_stale": True}, "mdi:cloud-alert"),
        ({"last_journey_cancelled": True}, "mdi:cancel"),
    ],
)
def test_live_activity_icon_matches_journey_status(changes, expected):
    assert _activity_icon(replace(_underway(), **changes)) == expected


def test_missing_delay_is_not_reported_as_on_time() -> None:
    assert _critical_delay_text(None) == "geen info"
    assert _critical_delay_text(None, "en") == "no info"


@pytest.mark.parametrize("delay", (-29, -9, 0, 9, 60))
def test_small_delay_is_reported_as_no_delay(delay: int) -> None:
    assert _critical_delay_text(delay) == "op tijd"


def test_stale_position_is_not_presented_as_current_location() -> None:
    message = _message(BusSnapshot(realtime_stale=True, current_stop="Gulpen"))
    assert message == "Laatst bekende halte: Gulpen"
    assert "Staat bij" not in message


def test_payload_has_no_line_header_or_forced_background() -> None:
    manager = _manager(BusSnapshot())
    scheduled = datetime(2026, 9, 15, 7, 41, tzinfo=UTC)
    payload = manager._payload(
        BusSnapshot(
            target_scheduled_time=scheduled,
            is_underway=True,
            delay_seconds=121,
            last_passed_stop="Gulpen",
        )
    )
    assert payload["title"] == "Bus"
    assert payload["title"] != "Home Assistant"
    assert "vertraging" not in payload["message"]
    assert "De bus van" not in payload["message"]
    assert payload["message"].endswith("Gepasseerd: Gulpen")
    assert "background_color" not in payload["data"]
    assert "text_color" not in payload["data"]


def test_message_uses_real_position_type() -> None:
    scheduled = datetime(2026, 9, 15, 7, 41, tzinfo=UTC)
    standing = BusSnapshot(
        target_scheduled_time=scheduled,
        is_underway=True,
        delay_seconds=121,
        current_stop="Oude Geul, Gulpen",
    )
    passed = BusSnapshot(
        target_scheduled_time=scheduled,
        is_underway=True,
        delay_seconds=121,
        last_passed_stop="Busstation, Gulpen",
    )

    assert _message(standing).endswith("Staat bij: Oude Geul, Gulpen")
    assert _message(passed).endswith("Gepasseerd: Busstation, Gulpen")
    assert _title(passed) == "Bus"


def test_missing_position_uses_both_lines() -> None:
    assert _message(BusSnapshot()) == "Geen bus onderweg"


def test_waiting_is_one_line_without_progress() -> None:
    snapshot = BusSnapshot(
        target_scheduled_time=datetime(2026, 9, 18, 11, 21, tzinfo=UTC),
        route_progress=0,
    )
    assert "\n" not in _message(snapshot)
    assert _message(snapshot).endswith(" · Wacht op vertrek")
    assert "progress" not in _manager(snapshot)._payload(snapshot)["data"]


@pytest.mark.parametrize(
    "delay,expected", [(121, "+2:01"), (30, "+30s"), (None, "waiting"), (0, "waiting")]
)
def test_waiting_bus_displays_reported_delay_on_right(delay, expected):
    snapshot = replace(_underway(), is_underway=False, delay_seconds=delay)
    manager = _manager(snapshot)
    manager._hass.config.language = "en"
    payload = manager._payload(snapshot)
    assert payload["data"]["critical_text"] == expected
    assert payload["message"].endswith("Waiting to depart")
    assert "progress" not in payload["data"]


def test_device_link_targets_only_configured_route() -> None:
    manager = _manager(BusSnapshot())
    with patch("custom_components.arriva_bus.live_activity.dr.async_get") as registry:
        registry.return_value.async_get_device.return_value = SimpleNamespace(id="route-device")
        assert manager._device_url() == "/config/devices/device/route-device"
        registry.return_value.async_get_device.assert_called_once_with(
            identifiers={("arriva_bus", "test")}
        )
        registry.return_value.async_get_device.return_value = None
        assert manager._device_url() == "/config/integrations/integration/arriva_bus"


def test_title_fallback_never_uses_companion_app_name() -> None:
    assert _title(BusSnapshot(destination="Groningen")) == "Bus ri. Groningen"


def test_live_activity_english_copy() -> None:
    snapshot = BusSnapshot(line="350", destination="Maastricht", delay_seconds=0)
    assert _title(snapshot, "en") == "Line 350 to Maastricht"
    assert _message(snapshot, "en") == "No bus underway"
    assert _critical_delay_text(60, "en") == "on time"


def _manager(snapshot: BusSnapshot) -> ArrivaLiveActivity:
    hass = MagicMock()
    hass.config.language = "nl"
    hass.async_create_task = lambda coro, name: asyncio.create_task(coro, name=name)
    manager = ArrivaLiveActivity(
        hass,
        SimpleNamespace(entry_id="test", data={}, options={}),
        SimpleNamespace(data=snapshot),
    )
    manager._async_send = AsyncMock(return_value=True)
    return manager


@pytest.mark.asyncio
async def test_underway_journey_starts_updates_and_ends_activity() -> None:
    snapshot = BusSnapshot(
        runtime_active=True,
        journey_number=123,
        is_underway=True,
        target_scheduled_time=datetime(2026, 9, 15, 7, 41, tzinfo=UTC),
        delay_seconds=121,
        last_passed_stop="Busstation, Gulpen",
    )
    manager = _manager(snapshot)

    await manager._async_sync()
    assert manager._active
    assert manager._async_send.await_count == 1

    await manager._async_sync()
    assert manager._async_send.await_count == 1

    manager._coordinator.data = BusSnapshot(runtime_active=False)
    await manager._async_sync()
    assert not manager._active
    manager._async_send.assert_awaited_with(
        "clear_notification",
        {"tag": "arriva_bus_test_v2"},
    )


@pytest.mark.asyncio
async def test_premature_end_retains_existing_activity_as_stale() -> None:
    snapshot = BusSnapshot(
        runtime_active=True,
        journey_number=123,
        realtime_stale=True,
        target_scheduled_time=datetime(2026, 9, 15, 7, 41, tzinfo=UTC),
        last_passed_stop="Busstation, Gulpen",
    )
    manager = _manager(snapshot)
    manager._active = True

    await manager._async_sync()

    assert manager._active
    sent_data = manager._async_send.await_args.args[1]
    assert sent_data["critical_text"] == "geen info"


def _underway() -> BusSnapshot:
    return BusSnapshot(
        runtime_active=True,
        journey_number=123,
        is_underway=True,
        delay_seconds=70,
        last_passed_stop="Busstation, Gulpen",
        target_scheduled_time=datetime(2026, 9, 16, 5, 41, tzinfo=UTC),
    )


@pytest.mark.parametrize("active", (False, True))
def test_start_reload_and_updates_always_request_silent_delivery(active: bool) -> None:
    manager = _manager(_underway())
    manager._active = active
    assert manager._payload(_underway())["data"]["silent"] is True


@pytest.mark.asyncio
async def test_delay_updates_keep_deadline_and_send_latest_without_new_event() -> None:
    manager = _manager(_underway())
    with patch("custom_components.arriva_bus.live_activity.monotonic") as clock:
        clock.return_value = 100.0
        await manager._async_sync()
        clock.return_value = 102.0
        manager._coordinator.data = replace(_underway(), delay_seconds=72)
        await manager._async_sync()
        delay, callback = manager._hass.loop.call_later.call_args.args
        assert delay == 58.0
        for second in range(103, 115):
            clock.return_value = float(second)
            manager._coordinator.data = replace(_underway(), delay_seconds=second)
            await manager._async_sync()
        # A stream of updates cannot postpone the deadline indefinitely.
        assert manager._hass.loop.call_later.call_count == 1
        assert manager._async_send.await_count == 1
        assert manager.diagnostics["pending_update"]
        clock.return_value = 160.0
        callback()
        await manager._task
        assert manager._async_send.await_count == 2
        assert manager._async_send.await_args.args[1]["critical_text"] == "+1:54"
        assert all(call.args[1]["silent"] is True for call in manager._async_send.await_args_list)
        assert not manager.diagnostics["pending_update"]


@pytest.mark.asyncio
async def test_position_change_bypasses_and_cancels_delay_timer() -> None:
    manager = _manager(_underway())
    with patch("custom_components.arriva_bus.live_activity.monotonic", return_value=100):
        await manager._async_sync()
        manager._coordinator.data = replace(_underway(), delay_seconds=80)
        await manager._async_sync()
        timer = manager._pending_sync
        manager._coordinator.data = replace(
            _underway(), delay_seconds=81, current_stop="Oude Geul, Gulpen"
        )
        await manager._async_sync()
    timer.cancel.assert_called_once()
    assert manager._async_send.await_count == 2
    assert manager._async_send.await_args.args[0].endswith("Staat bij: Oude Geul, Gulpen")
    assert manager._async_send.await_args.args[1]["critical_text"] == "+1:21"
    assert manager._async_send.await_args.args[1]["silent"] is True
    assert manager._pending_sync is None


@pytest.mark.asyncio
async def test_departure_from_same_stop_is_immediate() -> None:
    manager = _manager(replace(_underway(), current_stop="Busstation, Gulpen"))
    with patch("custom_components.arriva_bus.live_activity.monotonic", return_value=100):
        await manager._async_sync()
        manager._coordinator.data = _underway()
        await manager._async_sync()
    assert manager._async_send.await_count == 2
    assert manager._async_send.await_args.args[0].endswith("Gepasseerd: Busstation, Gulpen")


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ("realtime_stale", "delay_seconds"))
async def test_loss_and_recovery_of_information_are_immediate(field: str) -> None:
    manager = _manager(_underway())
    with patch("custom_components.arriva_bus.live_activity.monotonic", return_value=100):
        await manager._async_sync()
        manager._coordinator.data = replace(
            _underway(), **{field: True if field == "realtime_stale" else None}
        )
        await manager._async_sync()
        assert manager._async_send.await_args.args[1]["critical_text"] == "geen info"
        manager._coordinator.data = _underway()
        await manager._async_sync()
    assert manager._async_send.await_count == 3
    assert manager._pending_sync is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    ({"runtime_active": False}, {"target_has_passed": True}, {"last_journey_cancelled": True}),
)
async def test_end_is_immediate_and_cancels_pending_delay(changes: dict) -> None:
    manager = _manager(_underway())
    with patch("custom_components.arriva_bus.live_activity.monotonic", return_value=100):
        await manager._async_sync()
        manager._coordinator.data = replace(_underway(), delay_seconds=80)
        await manager._async_sync()
        timer = manager._pending_sync
        manager._coordinator.data = replace(_underway(), **changes)
        await manager._async_sync()
    timer.cancel.assert_called_once()
    assert manager._pending_sync is None
    if changes.get("runtime_active") is False:
        assert not manager._active
        manager._async_send.assert_awaited_with("clear_notification", {"tag": "arriva_bus_test_v2"})
    else:
        assert manager._active
        assert manager._async_send.await_args.args[0] != "clear_notification"


@pytest.mark.asyncio
async def test_return_to_last_sent_value_cancels_pending_update() -> None:
    manager = _manager(_underway())
    with patch("custom_components.arriva_bus.live_activity.monotonic", return_value=100):
        await manager._async_sync()
        manager._coordinator.data = replace(_underway(), delay_seconds=80)
        await manager._async_sync()
        timer = manager._pending_sync
        manager._coordinator.data = _underway()
        await manager._async_sync()
    timer.cancel.assert_called_once()
    assert manager._async_send.await_count == 1


@pytest.mark.asyncio
async def test_unload_cancels_pending_update_and_prevents_late_send() -> None:
    manager = _manager(_underway())
    with patch("custom_components.arriva_bus.live_activity.monotonic", return_value=100):
        await manager._async_sync()
        manager._coordinator.data = replace(_underway(), delay_seconds=80)
        await manager._async_sync()
        timer = manager._pending_sync
        callback = manager._hass.loop.call_later.call_args.args[1]
        await manager.async_stop()
        callback()
        await manager._async_sync()
    timer.cancel.assert_called_once()
    assert manager._async_send.await_count == 2
    manager._async_send.assert_awaited_with("clear_notification", {"tag": "arriva_bus_test_v2"})
    assert manager._task is None


@pytest.mark.asyncio
async def test_update_at_cooldown_boundary_sends_immediately() -> None:
    manager = _manager(_underway())
    with patch("custom_components.arriva_bus.live_activity.monotonic") as clock:
        clock.return_value = 100.0
        await manager._async_sync()
        clock.return_value = 160.0
        manager._coordinator.data = replace(_underway(), delay_seconds=85)
        await manager._async_sync()
    assert manager._async_send.await_count == 2
    manager._hass.loop.call_later.assert_not_called()


@pytest.mark.asyncio
async def test_diagnostics_distinguish_action_success_from_device_delivery() -> None:
    manager = _manager(_underway())
    # Exercise the real notify method, not the scheduling test's mock.
    del manager._async_send
    manager._notification_service = lambda: "mobile_app_test"
    manager._hass.services.async_call = AsyncMock()
    assert await manager._async_send("Position", {"live_update": True})
    assert manager.diagnostics["send_attempts"] == 1
    assert manager.diagnostics["notify_action_successes"] == 1
    assert manager.diagnostics["last_notify_action_success_at"]
    assert not manager.diagnostics["device_delivery_confirmed"]
    manager._hass.services.async_call.side_effect = HomeAssistantError("offline")
    assert not await manager._async_send("Position", {"live_update": True})
    assert manager.diagnostics["send_attempts"] == 2
    assert manager.diagnostics["notify_action_successes"] == 1
    assert manager.diagnostics["last_error"] == "notification_send_failed"
    manager._hass.services.async_call.side_effect = None
    assert await manager._async_send("Position", {"live_update": True})
    assert manager.diagnostics["last_error"] is None


@pytest.mark.asyncio
async def test_notify_timeout_is_recorded_without_marking_activity_sent() -> None:
    manager = _manager(_underway())
    del manager._async_send
    manager._notification_service = lambda: "mobile_app_test"
    manager._hass.services.async_call = AsyncMock(side_effect=TimeoutError)
    await manager._async_sync()
    assert not manager._active
    assert manager._last_payload is None
    assert manager.diagnostics["last_error"] == "notification_timeout"


@pytest.mark.asyncio
async def test_missing_notify_action_is_reported() -> None:
    manager = _manager(_underway())
    del manager._async_send
    manager._notification_service = lambda: None
    assert not await manager._async_send("Position", {})
    assert manager.diagnostics["last_error"] == "notification_action_unavailable"


@pytest.mark.asyncio
async def test_stop_during_send_does_not_create_a_trailing_update() -> None:
    manager = _manager(_underway())
    entered = asyncio.Event()
    release = asyncio.Event()

    async def send(*args, **kwargs):
        entered.set()
        await release.wait()
        return True

    manager._async_send.side_effect = send
    manager._schedule_sync()
    await entered.wait()
    manager._coordinator.data = replace(_underway(), delay_seconds=80)
    manager._schedule_sync()
    stop = asyncio.create_task(manager.async_stop())
    await asyncio.sleep(0)
    release.set()
    await stop
    assert manager._async_send.await_count == 2
    assert manager._pending_sync is None
    manager._hass.loop.call_later.assert_not_called()


@pytest.mark.asyncio
async def test_position_changed_during_send_is_sent_afterward() -> None:
    manager = _manager(_underway())
    entered = asyncio.Event()
    release = asyncio.Event()

    async def send(*args, **kwargs):
        entered.set()
        await release.wait()
        return True

    manager._async_send.side_effect = send
    manager._schedule_sync()
    await entered.wait()
    manager._coordinator.data = replace(_underway(), current_stop="Oude Geul, Gulpen")
    manager._schedule_sync()
    release.set()
    await manager._task
    assert manager._async_send.await_count == 2
    assert manager._async_send.await_args.args[0].endswith("Staat bij: Oude Geul, Gulpen")
    assert manager._pending_sync is None


@pytest.mark.asyncio
async def test_disable_clears_all_devices_even_without_local_active_state() -> None:
    manager = _manager(_underway())
    del manager._async_send
    manager._notification_services = lambda: ["mobile_app_one", "mobile_app_two"]
    manager._hass.services.async_call = AsyncMock()
    await manager.async_set_enabled(False)
    assert not manager.enabled
    assert manager._hass.services.async_call.await_count == 2
    for call in manager._hass.services.async_call.await_args_list:
        assert call.args[2]["message"] == "clear_notification"


@pytest.mark.asyncio
async def test_failure_on_one_device_does_not_skip_other_or_report_full_success() -> None:
    manager = _manager(_underway())
    del manager._async_send
    manager._notification_services = lambda: ["mobile_app_one", "mobile_app_two"]
    manager._hass.services.async_call = AsyncMock(side_effect=[HomeAssistantError(), None])
    assert not await manager._async_send("clear_notification", {"tag": "arriva_bus_test_v2"})
    assert manager._hass.services.async_call.await_count == 2
    assert manager.diagnostics["last_error"] == "notification_send_failed"


def test_progress_payload_uses_verified_value_and_omits_unknown_route() -> None:
    manager = _manager(_underway())
    for progress in (0, 50, 100):
        payload = manager._payload(replace(_underway(), route_progress=progress))
        assert payload["data"]["progress"] == progress
        assert payload["data"]["progress_max"] == 100
    assert "progress" not in manager._payload(_underway())["data"]


@pytest.mark.asyncio
async def test_new_journey_with_identical_position_and_delay_updates_same_activity():
    manager = _manager(BusSnapshot(runtime_active=True, is_underway=True, journey_key="trip1"))
    await manager._async_sync()
    manager._coordinator.data = replace(manager._coordinator.data, journey_key="trip2")
    await manager._async_sync()
    assert manager._async_send.await_count == 2
    assert all(call.args[0] != "clear_notification" for call in manager._async_send.await_args_list)
    assert (
        manager._async_send.await_args_list[0].kwargs["title"]
        == manager._async_send.await_args_list[1].kwargs["title"]
    )


@pytest.mark.asyncio
async def test_failed_send_retries_without_new_bus_event():
    manager = _manager(BusSnapshot(runtime_active=True, is_underway=True))
    manager._settings = {"mobile_device": ["iphone"]}
    manager._async_send.side_effect = [False, True]
    await manager._async_sync()
    assert not manager._active
    delay, callback = manager._hass.loop.call_later.call_args.args
    assert delay == 60
    callback()
    await manager._task
    assert manager._active
    assert manager._async_send.await_count == 2


def test_origin_departure_compact_position_and_on_time_color():
    snapshot = replace(
        _underway(),
        target_scheduled_time=None,
        route_stop_codes=("origin", "target"),
        last_passed_stop_code="origin",
    )
    assert _message(snapshot) == "Vertrokken vanaf: Busstation, Gulpen"
    assert (
        _message(replace(snapshot, last_passed_stop_code="middle"))
        == "Gepasseerd: Busstation, Gulpen"
    )
    assert _activity_color(-29, on_time_color="#FFFFFF") == "#FFFFFF"
    assert _activity_color(60, on_time_color="#FFFFFF") == "#FFFFFF"
    assert _activity_color(None, on_time_color="#FFFFFF") == "#9E9E9E"


@pytest.mark.asyncio
async def test_activity_survives_waiting_cancelled_passage_and_next_journey():
    manager = _manager(_underway())
    original_title = manager._payload(manager._coordinator.data)["title"]
    for snapshot in [
        _underway(),
        replace(_underway(), target_has_passed=True),
        BusSnapshot(runtime_active=True),
        replace(
            _underway(),
            is_underway=False,
            last_journey_cancelled=True,
            cancelled_scheduled_time=datetime(2026, 9, 16, 4, 41, tzinfo=UTC),
        ),
        replace(_underway(), journey_key="next", journey_number=124),
    ]:
        manager._coordinator.data = snapshot
        await manager._async_sync()
        assert manager._active
        assert manager._async_send.await_args.args[0] != "clear_notification"
        assert manager._async_send.await_args.kwargs["title"] == original_title
    messages = [call.args[0] for call in manager._async_send.await_args_list]
    assert any("uitgevallen\nVolgende:" in msg for msg in messages)
    assert any("Geen bus onderweg" in msg for msg in messages)


@pytest.mark.asyncio
async def test_options_change_clears_only_removed_phone_and_updates_remaining():
    manager = _manager(_underway())
    manager._settings = {"mobile_device": ["one", "two"]}
    manager._notification_services = lambda: manager._settings["mobile_device"]
    await manager._async_sync()
    manager._async_send.reset_mock()
    await manager.async_update_settings({"mobile_device": ["two", "three"], "delay_color": "blue"})
    await manager._task
    clear = manager._async_send.await_args_list[0]
    assert clear.args[0] == "clear_notification"
    assert clear.kwargs["services_override"] == ["one"]
    assert manager._settings["mobile_device"] == ["two", "three"]
    assert manager._async_send.await_args.args[1]["notification_icon_color"] == "#2196F3"


@pytest.mark.asyncio
async def test_failed_removed_phone_clear_preserves_settings():
    manager = _manager(_underway())
    manager._settings = {"mobile_device": ["one"]}
    manager._notification_services = lambda: manager._settings["mobile_device"]
    manager._async_send.return_value = False
    with pytest.raises(HomeAssistantError):
        await manager.async_update_settings({"mobile_device": []})
    assert manager._settings["mobile_device"] == ["one"]


@pytest.mark.parametrize(
    "language,label,short",
    [
        ("nl", "Gegevens laden…", "laden"),
        ("en", "Loading data…", "loading"),
    ],
)
def test_loading_is_distinct_from_waiting(language, label, short):
    snapshot = BusSnapshot(runtime_active=True, is_loading=True, journey_number=17)
    manager = _manager(snapshot)
    with patch("custom_components.arriva_bus.live_activity._language", return_value=language):
        payload = manager._payload(snapshot)
    assert payload["message"] == label
    assert payload["data"]["critical_text"] == short
    assert payload["data"]["notification_icon"] == "mdi:progress-clock"
    assert payload["data"]["notification_icon_color"] == "#9E9E9E"
    assert "progress" not in payload["data"]
