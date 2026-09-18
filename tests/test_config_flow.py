"""Dynamic route discovery and isolation regressions for Arriva Bus."""

import asyncio
from collections import deque
from dataclasses import asdict, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.arriva_bus.api import TransitHttpClient
from custom_components.arriva_bus.catalog import CatalogError, RouteCatalog
from custom_components.arriva_bus.config_flow import ArrivaConfigFlow
from custom_components.arriva_bus.coordinator import ArrivaCoordinator
from custom_components.arriva_bus.live_activity import ArrivaLiveActivity
from custom_components.arriva_bus.models import (
    BusSnapshot,
    JourneySelection,
    Kv6Event,
    parse_drgl_departures,
)
from custom_components.arriva_bus.realtime import Kv6Subscriber
from custom_components.arriva_bus.route_config import RouteConfig
from custom_components.arriva_bus.sensor import SENSORS, ArrivaSensor
from custom_components.arriva_bus.switch import ArrivaLiveActivitySwitch, ArrivaRuntimeSwitch

ROUTE = RouteConfig("27001", "1", "Centrum", "NL:S:12345678", "Station, Teststad")


def test_color_labels_and_threshold_descriptions_exist_in_both_languages():
    import json
    from pathlib import Path

    for filename in ("strings.json", "translations/en.json", "translations/nl.json"):
        translations = json.loads((Path("custom_components/arriva_bus") / filename).read_text())
        for step in (
            translations["config"]["step"]["devices"],
            translations["options"]["step"]["init"],
        ):
            for field in ("delay_color", "early_color", "on_time_color"):
                assert step["data"][field]
                assert step["data_description"][field]
            assert "30" in step["data_description"]["early_color"]
            assert "10" not in step["data_description"]["early_color"]
            assert any(
                word in step["data_description"]["on_time_color"].lower()
                for word in ("white", "wit")
            )


def departure(planning="27001", direction="Centrum", number="1", owner="ARR", journey=17):
    return f"""<a href="/journey/{owner}:{planning}:{journey}/20260917/">
        <div class="ott-linecode">{number}</div>
        <div class="ott-destination">{direction}</div>
        <div class="ott-departure-time">14:41</div></a>"""


BOARD = "<title>Station, Teststad - Vertrektijden</title>" + "".join(
    [
        departure(),
        departure(journey=18),
        departure(direction="Centrum via Dorp"),
        departure(direction="Buitenwijk"),
        departure(planning="99999", direction="Centrum"),
        departure(owner="CXX"),
    ]
)


def sample_catalog():
    return RouteCatalog(
        {
            "schema": 1,
            "valid_until": "2099-01-01",
            "feed_version": "test",
            "stops": {ROUTE.target_stop_code: ROUTE.target_stop_name},
            "lines": [
                {
                    "id": ROUTE.line_planning_number,
                    "number": ROUTE.line_public_number,
                    "name": "Station - Centrum",
                    "directions": [{"name": ROUTE.destination, "stops": [ROUTE.target_stop_code]}],
                }
            ],
        }
    )


def test_journey_filter_does_not_mix_same_public_number_or_similar_destination():
    journeys = parse_drgl_departures(
        BOARD, destination="Centrum", line="1", line_planning_number="27001"
    )
    assert len(journeys) == 2
    assert journeys[0].key == "ARR:27001:17/20260917"
    assert journeys[0].scheduled_target.hour == 14
    assert (
        parse_drgl_departures(BOARD, destination="Missing", line="1", line_planning_number="27001")
        == []
    )


@pytest.mark.asyncio
async def test_http_runtime_uses_selected_stop_and_internal_line():
    client = TransitHttpClient(MagicMock(), ROUTE)
    client._get_text_url = AsyncMock(return_value=BOARD)
    journeys = await client.async_get_journeys()
    client._get_text_url.assert_awaited_once_with("https://drgl.nl/stop/NL:S:12345678")
    assert journeys[0].line_planning_number == "27001"
    client._get_text_url = AsyncMock(
        return_value="""<div id="ott-main-journeycalls">
      <a href="/stop/NL:S:origin">Origin</a><a href="/stop/NL:S:12345678">Target</a>
      <a href="/stop/NL:S:beyond">Beyond</a></div>"""
    )
    assert await client.async_get_route(journeys[0]) == ("NL:S:origin", "NL:S:12345678")
    client._get_text_url.assert_awaited_once_with("https://drgl.nl/journey/ARR:27001:17/20260917/")


def flow():
    item = ArrivaConfigFlow()
    item.hass = MagicMock()
    item.context = {}
    item.async_set_unique_id = AsyncMock()
    item._abort_if_unique_id_configured = MagicMock()
    return item


@pytest.mark.asyncio
async def test_full_config_flow_persists_discovered_route_without_technical_input():
    item = flow()
    with patch(
        "custom_components.arriva_bus.config_flow.async_load_catalog",
        new_callable=AsyncMock,
        return_value=sample_catalog(),
    ):
        first = await item.async_step_user()
        assert first["step_id"] == "user"
        assert {key.schema for key in first["data_schema"].schema} == {"line"}
        assert (await item.async_step_user({"line": ROUTE.line_planning_number}))[
            "step_id"
        ] == "direction"
        assert (await item.async_step_direction({"direction": ROUTE.destination}))[
            "step_id"
        ] == "stop"
        assert (await item.async_step_stop({"stop": ROUTE.target_stop_code}))[
            "step_id"
        ] == "devices"
        result = await item.async_step_devices(
            {"delay_color": [10, 20, 30], "early_color": [255, 0, 0]}
        )
    assert result["data"] == {
        **asdict(ROUTE),
        "destination_aliases": ("Centrum",),
        "mobile_device": [],
        "initial_active": True,
        "delay_color": [10, 20, 30],
        "early_color": [255, 0, 0],
    }
    assert result["title"] == ROUTE.title
    item.async_set_unique_id.assert_awaited_with(ROUTE.unique_id)
    item._abort_if_unique_id_configured.assert_called()


@pytest.mark.asyncio
async def test_config_handles_catalog_failure_retry_and_invalid_selections():
    item = flow()
    with patch(
        "custom_components.arriva_bus.config_flow.async_load_catalog", new_callable=AsyncMock
    ) as loader:
        loader.side_effect = CatalogError("no valid catalogue")
        assert (await item.async_step_user())["errors"] == {"base": "catalog_unavailable"}
        loader.side_effect = None
        loader.return_value = sample_catalog()
        assert (await item.async_step_user({}))["step_id"] == "user"
        assert (await item.async_step_user({"line": "invalid"}))["errors"] == {
            "base": "invalid_selection"
        }
        await item.async_step_user({"line": ROUTE.line_planning_number})
        assert (await item.async_step_direction({"direction": "invalid"}))["errors"] == {
            "base": "invalid_selection"
        }
        await item.async_step_direction({"direction": ROUTE.destination})
        assert (await item.async_step_stop({"stop": "NL:S:injected"}))["errors"] == {
            "base": "invalid_selection"
        }


def coordinator(route=ROUTE, entry_id="route-a"):
    obj = object.__new__(ArrivaCoordinator)
    obj.route, obj.entry_id = route, entry_id
    obj._active = JourneySelection(17, "2026-09-17", None, route.line_planning_number)
    obj._runtime_active, obj._shutdown = True, False
    obj._kv6 = None
    obj._recent_events = deque()
    obj._passed_journeys, obj._invalid_journeys = {}, {}
    obj._request_maintenance = lambda **_: None
    obj._last_event_order_timestamp = obj._last_stop_progress_timestamp = None
    obj._stop_place_by_user_stop = {"start": "NL:S:origin", "target": route.target_stop_code}
    obj._client = MagicMock()
    obj._client.get_cached_stop_name.side_effect = lambda code: code
    obj._schedule_stop_name_resolution = lambda *_: None
    obj.async_set_updated_data = lambda data: setattr(obj, "data", data)
    obj.data = BusSnapshot(
        runtime_active=True,
        line=route.line_public_number,
        destination=route.destination,
        journey_number=17,
        operating_day="2026-09-17",
        journey_key=obj._active.key,
        route_stop_codes=("NL:S:origin", route.target_stop_code),
    )
    return obj


def event(planning="27001", stop="start", event_type="DEPARTURE"):
    now = datetime(2026, 9, 17, 12, tzinfo=UTC)
    return Kv6Event(
        event_type, "ARR", planning, "2026-09-17", 17, 0, stop, 1, now, "VEHICLE", 121, "42", now
    )


@pytest.mark.asyncio
async def test_dynamic_realtime_isolation_and_progress_to_selected_target():
    obj = coordinator()
    await obj._async_handle_kv6_event(event(planning="26057"))
    assert len(obj._recent_events) == 0
    await obj._async_handle_kv6_event(event())
    assert obj.data.is_underway and obj.data.route_progress == 0
    assert obj.data.delay_seconds == 121
    from datetime import timedelta

    arrival = event(stop="target", event_type="ARRIVAL")
    arrival = replace(
        arrival,
        timestamp=arrival.timestamp + timedelta(minutes=1),
        received_at=arrival.received_at + timedelta(minutes=1),
    )
    await obj._async_handle_kv6_event(arrival)
    assert obj.data.current_stop_code == ROUTE.target_stop_code
    assert obj.data.route_progress == 100
    assert Kv6Subscriber("27001")._line_planning_number == "27001"


def managers():
    result = []
    for key in ("route-a", "route-b"):
        obj = coordinator(entry_id=key)
        entry = SimpleNamespace(entry_id=key, data=asdict(ROUTE), options={})
        hass = MagicMock()
        hass.config.language = "nl"
        result.append((obj, ArrivaLiveActivity(hass, entry, obj)))
    return result


def test_entities_devices_and_live_activity_tags_do_not_collide():
    first, second = managers()
    for obj, live in (first, second):
        runtime_switch = ArrivaRuntimeSwitch(obj)
        live_switch = ArrivaLiveActivitySwitch(live)
        sensor = ArrivaSensor(obj, SENSORS[0])
        assert (
            sensor.device_info["identifiers"]
            == runtime_switch.device_info["identifiers"]
            == live_switch.device_info["identifiers"]
        )
        assert obj.entry_id in sensor.unique_id
        payload = live._payload(replace(obj.data, delay_seconds=121, is_underway=True))
        assert payload["data"]["critical_text"] == "+2:01"
        assert payload["data"]["silent"] is True
    assert first[1]._tag != second[1]._tag
    assert first[1]._payload(first[0].data)["title"] == "Lijn 1 ri. Centrum"


@pytest.mark.asyncio
async def test_stop_and_disable_clear_only_own_activity():
    first, second = managers()
    for obj, live in (first, second):
        live._async_send = AsyncMock(return_value=True)
    await first[1].async_set_enabled(False)
    first[1]._async_send.assert_awaited_with("clear_notification", {"tag": "arriva_bus_route-a_v2"})
    second[1]._async_send.assert_not_awaited()
    await second[1].async_stop()
    second[1]._async_send.assert_awaited_with(
        "clear_notification", {"tag": "arriva_bus_route-b_v2"}
    )


@pytest.mark.asyncio
async def test_runtime_still_uses_only_manual_switch():
    obj = coordinator()
    obj._runtime_lock = asyncio.Lock()
    obj.enabled = True
    obj._inactive_reason = None
    obj._async_deactivate = AsyncMock()
    await obj.async_set_enabled(False)
    obj._async_deactivate.assert_awaited_once_with("manually_disabled")
    assert obj._runtime_reason() == "manually_disabled"


@pytest.mark.asyncio
@pytest.mark.parametrize("previous, should_start", [(None, True), ("on", True), ("off", False)])
async def test_new_tracker_starts_but_restored_off_stays_off(previous, should_start):
    obj = coordinator()
    obj.async_set_enabled = AsyncMock()
    switch = ArrivaRuntimeSwitch(obj)
    switch.async_get_last_state = AsyncMock(
        return_value=None if previous is None else SimpleNamespace(state=previous)
    )
    with patch(
        "custom_components.arriva_bus.switch.CoordinatorEntity.async_added_to_hass",
        new_callable=AsyncMock,
    ):
        await switch.async_added_to_hass()
    if should_start:
        obj.async_set_enabled.assert_awaited_once_with(True)
    else:
        obj.async_set_enabled.assert_not_awaited()


def test_line_number_title_and_custom_colors_keep_exact_thresholds():
    obj, live = managers()[0]
    live._settings = {"delay_color": [0, 0, 255], "early_color": [255, 128, 0]}
    scheduled = datetime(2026, 9, 17, 11, 20, tzinfo=UTC)
    data = replace(obj.data, line="350", target_scheduled_time=scheduled, is_underway=True)
    payload = live._payload(data)
    assert payload["title"] == "Lijn 350 ri. Centrum"
    for seconds, color in [
        (60, "#FFFFFF"),
        (61, "#0000FF"),
        (-9, "#FFFFFF"),
        (-29, "#FFFFFF"),
        (-30, "#FF8000"),
    ]:
        payload = live._payload(replace(data, delay_seconds=seconds))
        assert payload["data"]["notification_icon_color"] == color
        assert payload["data"]["progress_bar_color"] == color


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["ARRIVAL", "ONSTOP", "DEPARTURE", "ONROUTE"])
async def test_planned_dwell_does_not_claim_early_departure(event_type):
    from datetime import timedelta

    from custom_components.arriva_bus.models import ScheduledDwell

    obj = coordinator()
    sample = replace(event(event_type=event_type), punctuality=-300)
    call = ScheduledDwell(
        "NL:S:origin",
        sample.timestamp - timedelta(minutes=1),
        sample.timestamp + timedelta(minutes=5),
    )
    obj.data = replace(obj.data, is_underway=True, route_dwell_calls=(call,))
    await obj._async_handle_kv6_event(sample)
    assert obj.data.delay_seconds == (0 if event_type in ("ARRIVAL", "ONSTOP") else -300)
    assert obj.data.raw_delay_seconds == -300
    assert bool(obj.data.scheduled_wait_until) == (event_type in ("ARRIVAL", "ONSTOP"))


@pytest.mark.asyncio
async def test_unknown_dwell_late_arrival_and_departure_preserve_raw_punctuality():
    from datetime import timedelta

    from custom_components.arriva_bus.models import ScheduledDwell

    obj = coordinator()
    obj.data = replace(obj.data, is_underway=True)
    sample = replace(event(event_type="ONSTOP"), punctuality=-120)
    await obj._async_handle_kv6_event(sample)
    assert obj.data.delay_seconds == -120  # No timetable proof: do not guess.
    obj.data = replace(
        obj.data,
        route_dwell_calls=(
            ScheduledDwell(
                "NL:S:origin",
                sample.timestamp - timedelta(minutes=5),
                sample.timestamp - timedelta(minutes=1),
            ),
        ),
    )
    sample = replace(sample, punctuality=61, timestamp=sample.timestamp + timedelta(seconds=1))
    await obj._async_handle_kv6_event(sample)
    assert obj.data.delay_seconds == 61
    assert obj.data.scheduled_wait_until is None


def test_long_planned_dwell_does_not_trigger_false_journey_rejection():
    from datetime import timedelta

    from custom_components.arriva_bus.models import ScheduledDwell

    obj = coordinator()
    sample = replace(event(event_type="ONSTOP"), punctuality=-900)
    obj.data = replace(
        obj.data,
        is_underway=True,
        route_dwell_calls=(
            ScheduledDwell(
                "NL:S:origin", sample.timestamp, sample.timestamp + timedelta(minutes=15)
            ),
        ),
    )
    assert not obj._is_implausibly_early(sample)
    assert obj._is_implausibly_early(replace(sample, event_type="DEPARTURE"))


def test_dwell_parser_uses_planned_times_and_handles_midnight():
    from custom_components.arriva_bus.models import parse_drgl_dwell_calls

    html = """<div id="ott-main-journeycalls"><a href="/stop/NL:S:origin">
    <span class="ott-call-arrivaltime">23:58 +3</span><br>
    <span class="ott-call-departuretime">00:05 -1</span></a>
    <a href="/stop/NL:S:next"><span class="ott-call-arrivaltime">00:10</span></a>"""
    calls = parse_drgl_dwell_calls(html, "2026-09-17")
    assert len(calls) == 1
    assert calls[0].arrival.day == 17 and calls[0].departure.day == 18
    assert int((calls[0].departure - calls[0].arrival).total_seconds()) == 420


def test_known_stop_headsign_alias_matches_without_accepting_other_routes():
    board = departure(direction="Maastricht Station", planning="26057", number="57")
    board += departure(direction="Gulpen", planning="26057", number="57")
    board += departure(direction="Maastricht Station", planning="99999", number="57")
    journeys = parse_drgl_departures(
        board,
        destination="Maastricht",
        line="57",
        line_planning_number="26057",
        destination_aliases=("Maastricht Station",),
    )
    assert len(journeys) == 1
    assert journeys[0].key == "ARR:26057:17/20260917"


def test_named_color_dropdown_preserves_legacy_custom_color():
    from custom_components.arriva_bus.colors import color_hex
    from custom_components.arriva_bus.config_flow import _settings_schema

    schema = _settings_schema({"delay_color": [10, 20, 30], "early_color": [244, 67, 54]})
    assert schema({}) == {}  # Clearing must not reinsert suggested values.
    values = {
        str(key): key.description["suggested_value"] for key in schema.schema if key.description
    }
    assert values["delay_color"] == "#0A141E"
    assert values["early_color"] == "red"
    assert color_hex(values["delay_color"], "fallback") == "#0A141E"
    assert color_hex("blue", "fallback") == "#2196F3"
    for key, selector in schema.schema.items():
        if str(key) in ("delay_color", "early_color"):
            assert selector.config["mode"] == "dropdown"
            assert "green" in selector.config["options"]


@pytest.mark.asyncio
async def test_setup_enriches_legacy_destination_without_changing_entry_identity():
    import gzip
    import json
    from pathlib import Path

    from custom_components.arriva_bus import async_setup_entry
    from custom_components.arriva_bus.catalog import BUNDLED_CATALOG

    data = json.loads(gzip.decompress(Path(BUNDLED_CATALOG).read_bytes()))
    catalog = RouteCatalog(data)
    code = next(
        code
        for code in catalog.stops("26057", "Maastricht")
        if "Maastricht Station" in catalog.aliases("26057", "Maastricht", code)
    )
    old_data = {
        "line_planning_number": "26057",
        "line_public_number": "57",
        "destination": "Maastricht Station",
        "target_stop_code": code,
        "target_stop_name": "Station",
    }
    entry = SimpleNamespace(data=old_data)
    hass = MagicMock()

    async def executor(fn, *args):
        return fn(*args)

    hass.async_add_executor_job = executor
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    with (
        patch("custom_components.arriva_bus.async_get_clientsession"),
        patch("custom_components.arriva_bus.TransitHttpClient") as client,
        patch("custom_components.arriva_bus.ArrivaCoordinator", return_value=AsyncMock()),
        patch("custom_components.arriva_bus.ArrivaLiveActivity", return_value=AsyncMock()),
    ):
        assert await async_setup_entry(hass, entry)
    runtime_route = client.call_args.args[1]
    assert "Maastricht" in runtime_route.destination_aliases
    assert "Maastricht Station" in runtime_route.destination_aliases
    assert entry.data is old_data
    assert "destination_aliases" not in entry.data


@pytest.mark.asyncio
async def test_first_setup_starts_active_and_consumes_one_shot_marker():
    from custom_components.arriva_bus import async_setup_entry

    entry = SimpleNamespace(
        entry_id="new-route",
        data={**asdict(ROUTE), "initial_active": True},
        options={},
    )
    hass = MagicMock()

    async def executor(fn, *args):
        return fn(*args)

    hass.async_add_executor_job = executor
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    coordinator = AsyncMock()
    with (
        patch("custom_components.arriva_bus.async_get_clientsession"),
        patch(
            "custom_components.arriva_bus.decode_catalog",
            return_value=SimpleNamespace(aliases=lambda *args: (ROUTE.destination,)),
        ),
        patch("custom_components.arriva_bus.TransitHttpClient"),
        patch("custom_components.arriva_bus.ArrivaCoordinator", return_value=coordinator),
        patch("custom_components.arriva_bus.ArrivaLiveActivity", return_value=AsyncMock()),
    ):
        assert await async_setup_entry(hass, entry)

    coordinator.async_set_enabled.assert_awaited_once_with(True)
    updated = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert "initial_active" not in updated


@pytest.mark.asyncio
async def test_already_underway_next_bus_is_replayed_after_target_passage():
    from datetime import timedelta

    obj = coordinator()
    obj._client.async_get_journeys = AsyncMock(
        return_value=[JourneySelection(18, "2026-09-17", None, "27001")]
    )
    obj._client.async_get_route = AsyncMock(return_value=("NL:S:origin", ROUTE.target_stop_code))
    obj._client.get_dwell_calls.return_value = ()
    await obj._async_handle_kv6_event(event())
    next_bus = replace(
        event(), journey_number=18, timestamp=event().timestamp + timedelta(seconds=30)
    )
    await obj._async_handle_kv6_event(next_bus)
    assert obj.data.journey_number == 17
    passed = replace(event(stop="target"), timestamp=event().timestamp + timedelta(minutes=1))
    await obj._async_handle_kv6_event(passed)
    await obj._async_refresh_journeys(passed.timestamp)
    assert obj.data.journey_number == 18
    assert obj.data.is_underway
    assert obj.data.last_passed_stop_code == "NL:S:origin"


@pytest.mark.asyncio
async def test_cleared_colors_are_saved_as_resets_without_reloading():
    from custom_components.arriva_bus.config_flow import ArrivaOptionsFlow

    options = ArrivaOptionsFlow()
    entry = MagicMock()
    entry.data = {"delay_color": "blue", "early_color": "purple"}
    entry.runtime_data.live_activity.async_update_settings = AsyncMock()
    with patch.object(
        ArrivaOptionsFlow, "config_entry", new_callable=lambda: property(lambda _: entry)
    ):
        options.hass = MagicMock()
        options.context = {}
        result = await options.async_step_init({})
    assert result["data"] == {
        "mobile_device": [],
        "delay_color": "default",
        "early_color": "default",
        "on_time_color": "default",
    }
    entry.runtime_data.live_activity.async_update_settings.assert_awaited_once_with(result["data"])
