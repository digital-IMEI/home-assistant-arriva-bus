"""Data models and parsing helpers for Arriva Bus (test)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from html import unescape
from io import BytesIO
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

AMSTERDAM = ZoneInfo("Europe/Amsterdam")


@dataclass(frozen=True, slots=True)
class JourneySelection:
    """An Arriva journey selected at the configured target stop."""

    journey_number: int
    operating_day: str
    scheduled_target: datetime | None
    line_planning_number: str
    cancelled: bool = False

    @property
    def key(self) -> str:
        """Stable journey key for diagnostics and matching."""
        return f"ARR:{self.line_planning_number}:{self.journey_number}/{self.operating_day.replace('-', '')}"


@dataclass(frozen=True, slots=True)
class Kv6Event:
    """One BISON KV6 event."""

    event_type: str
    data_owner_code: str | None
    line_planning_number: str | None
    operating_day: str | None
    journey_number: int | None
    reinforcement_number: int | None
    user_stop_code: str | None
    passage_sequence_number: int | None
    timestamp: datetime | None
    source: str | None
    punctuality: int | None
    vehicle_number: str | None
    received_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ScheduledDwell:
    """Explicit planned waiting interval at one stop on this particular trip."""

    stop_code: str
    arrival: datetime
    departure: datetime


@dataclass(frozen=True, slots=True)
class BusSnapshot:
    """Current state exposed by the integration."""

    journey_number: int | None = None
    operating_day: str | None = None
    journey_key: str | None = None

    line: str = ""
    destination: str = ""

    runtime_active: bool = False
    inactive_reason: str | None = None

    # True only after a trusted vehicle-origin KV6 event proves that the
    # selected trip is physically being executed.
    is_underway: bool = False

    delay_seconds: int | None = None
    raw_delay_seconds: int | None = None
    scheduled_wait_until: datetime | None = None
    delay_source_stop: str | None = None
    delay_source_status: str | None = None
    delay_observed_at: datetime | None = None

    last_passed_stop: str | None = None
    # National CHB StopPlaceCode, not the operator-domain KV6 UserStopCode.
    last_passed_stop_code: str | None = None

    # ARRIVAL/ONSTOP means the vehicle is currently standing at this stop.
    # DEPARTURE/ONROUTE clears this position and advances last_passed_stop.
    current_stop: str | None = None
    current_stop_code: str | None = None

    target_scheduled_time: datetime | None = None
    target_has_passed: bool = False
    route_stop_codes: tuple[str, ...] = ()
    route_dwell_calls: tuple[ScheduledDwell, ...] = ()
    route_progress: int | None = None

    # The source timestamp is useful for ordering and diagnostics; freshness is
    # based on our own receipt time so a skewed vehicle clock cannot keep a bus
    # active indefinitely.
    data_timestamp: datetime | None = None
    last_received_at: datetime | None = None
    last_event_type: str | None = None
    vehicle_number: str | None = None
    realtime_connected: bool = False
    realtime_stale: bool = False
    last_journey_cancelled: bool = False
    # Scheduled target-stop passage of the most recently cancelled journey.
    # Kept separately because target_scheduled_time belongs to the next journey.
    cancelled_scheduled_time: datetime | None = None


class ParseError(ValueError):
    """Raised when public transport data cannot be interpreted."""


_JOURNEY_BLOCK_RE = re.compile(
    r"<a\b(?=[^>]*\bhref=[\"']/journey/ARR:(?P<planning>[A-Za-z0-9_-]+):"
    r"(?P<journey>\d+)/(?P<day>\d{8})/[\"'])[^>]*>"
    r"(?P<body>.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
_DESTINATION_RE = re.compile(
    r"class=[\"'][^\"']*\bott-destination\b[^\"']*[\"'][^>]*>"
    r"(?P<value>.*?)</div>",
    re.IGNORECASE | re.DOTALL,
)
_LINE_RE = re.compile(
    r"class=[\"'][^\"']*\bott-linecode\b[^\"']*[\"'][^>]*>"
    r"(?P<value>.*?)</div>",
    re.IGNORECASE | re.DOTALL,
)
_TIME_RE = re.compile(
    r"class=[\"'][^\"']*\bott-departure-time\b[^\"']*[\"'][^>]*>"
    r"(?P<value>.*?)</div>",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_CLOCK_RE = re.compile(r"\b(?P<hour>[01]?\d|2[0-3]):(?P<minute>[0-5]\d)\b")
_TITLE_RE = re.compile(r"<title>(?P<title>.*?)</title>", re.IGNORECASE | re.DOTALL)
# A valid public stop display name must contain a letter. This deliberately
# rejects an accidentally requested numeric UserStopCode page such as
# "66430270 - Vertrektijden".
_STOP_NAME_HAS_LETTER_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]")
_PSA_FILENAME_RE = re.compile(
    r"PassengerStopAssignmentExportCHB_(?P<day>\d{4}-\d{2}-\d{2})\.xml\.gz",
    re.IGNORECASE,
)


def _clean_html(value: str) -> str:
    return " ".join(unescape(_TAG_RE.sub(" ", value)).split())


def parse_drgl_departures(
    html: str,
    *,
    destination: str,
    line: str,
    line_planning_number: str,
    destination_aliases: tuple[str, ...] = (),
) -> list[JourneySelection]:
    """Parse journeys for the exact configured internal line and destination."""
    result: list[JourneySelection] = []
    wanted_destinations = {name.casefold() for name in (destination, *destination_aliases)}

    for match in _JOURNEY_BLOCK_RE.finditer(html):
        if match.group("planning") != line_planning_number:
            continue
        body = match.group("body")
        block = match.group(0)
        normalized_body = _clean_html(body).casefold()
        raw_block = block.casefold()

        if "vertrokken" in normalized_body:
            continue

        cancelled = any(
            marker in normalized_body or marker in raw_block
            for marker in ("vervallen", "cancelled", "canceled")
        )

        dest_match = _DESTINATION_RE.search(body)
        line_match = _LINE_RE.search(body)
        if not dest_match or not line_match:
            continue

        destination_value = _clean_html(dest_match.group("value"))
        line_value = _clean_html(line_match.group("value"))
        if destination_value.casefold() not in wanted_destinations or line_value != line:
            continue

        journey_number = int(match.group("journey"))
        day_text = match.group("day")
        operating_day = f"{day_text[0:4]}-{day_text[4:6]}-{day_text[6:8]}"

        scheduled: datetime | None = None
        time_match = _TIME_RE.search(body)
        if time_match:
            display_time = _clean_html(time_match.group("value"))
            clock_match = _CLOCK_RE.search(display_time)
            if clock_match:
                scheduled = datetime(
                    int(day_text[0:4]),
                    int(day_text[4:6]),
                    int(day_text[6:8]),
                    int(clock_match.group("hour")),
                    int(clock_match.group("minute")),
                    tzinfo=AMSTERDAM,
                )

        result.append(
            JourneySelection(
                journey_number=journey_number,
                operating_day=operating_day,
                scheduled_target=scheduled,
                line_planning_number=line_planning_number,
                cancelled=cancelled,
            )
        )

    result.sort(
        key=lambda item: (
            item.scheduled_target.timestamp() if item.scheduled_target is not None else float("inf")
        )
    )
    return result


def parse_drgl_stop_name(html: str) -> str | None:
    """Extract a real human-readable stop name from a DRGL stop page."""
    match = _TITLE_RE.search(html)
    if not match:
        return None

    title = _clean_html(match.group("title"))
    for suffix in (" - Vertrektijden", " – Vertrektijden", " ‐ Vertrektijden"):
        if title.endswith(suffix):
            title = title[: -len(suffix)]
            break

    title = title.strip(" ,;:-–—\t\r\n")
    if not title or not _STOP_NAME_HAS_LETTER_RE.search(title):
        return None

    if title.casefold() in {"unknown", "onbekend"}:
        return None

    return title


def select_latest_psa_filename(index_html: str, today: date) -> str | None:
    """Select the newest PassengerStopAssignment export not newer than today."""
    found: dict[date, str] = {}

    for match in _PSA_FILENAME_RE.finditer(index_html):
        try:
            file_day = date.fromisoformat(match.group("day"))
        except ValueError:
            continue
        found[file_day] = match.group(0)

    if not found:
        return None

    eligible = [file_day for file_day in found if file_day <= today]
    selected_day = max(eligible) if eligible else min(found)
    return found[selected_day]


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(element: ET.Element, name: str) -> str | None:
    wanted = name.casefold()
    for child in element:
        if _local_name(child.tag).casefold() == wanted:
            text = child.text.strip() if child.text else ""
            return text or None
    return None


def _as_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _as_date(value: str | None) -> date | None:
    if not value:
        return None

    # PassengerStopAssignment uses an XML date-time value (for example
    # ``2016-12-11 00:00:00``), although the field represents a calendar date.
    # ``date.fromisoformat`` rejects that value when it is passed in full.  The
    # previous parser consequently discarded every current Arriva assignment,
    # which left the last-passed-stop sensor permanently empty.
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def parse_passenger_stop_assignments(
    xml_bytes: bytes,
    *,
    data_owner: str,
    valid_on: date,
) -> dict[str, str]:
    """Map operator UserStopCode -> national CHB StopPlaceCode.

    The current BISON PSA schema places ``stopplacecode`` directly under each
    ``quay`` and the operator mappings under
    ``quay/userstopcodes/userstopcodedata``. A ``stopplace`` can also contain
    user-stop mappings, so both container types are supported.

    Only mappings valid on ``valid_on`` are returned. If the export contains
    successive assignments for the same UserStopCode, the assignment with the
    latest valid-from date wins.
    """
    candidates: dict[str, tuple[date, str]] = {}

    try:
        iterator = ET.iterparse(BytesIO(xml_bytes), events=("end",))
        for _, element in iterator:
            kind = _local_name(element.tag).casefold()
            if kind not in {"quay", "stopplace"}:
                continue

            stop_place_code = _child_text(element, "stopplacecode")
            if not stop_place_code:
                element.clear()
                continue

            for assignment in element.iter():
                if _local_name(assignment.tag).casefold() != "userstopcodedata":
                    continue

                owner = _child_text(assignment, "dataownercode")
                if owner != data_owner:
                    continue

                user_stop_code = _child_text(assignment, "userstopcode")
                valid_from = _as_date(_child_text(assignment, "validfrom"))
                valid_thru = _as_date(_child_text(assignment, "validthru"))

                if not user_stop_code or valid_from is None:
                    continue
                if valid_from > valid_on:
                    continue
                if valid_thru is not None and valid_on > valid_thru:
                    continue

                existing = candidates.get(user_stop_code)
                if existing is None or valid_from > existing[0]:
                    candidates[user_stop_code] = (valid_from, stop_place_code)

            element.clear()

    except ET.ParseError as err:
        raise ParseError(f"Invalid PassengerStopAssignment XML: {err}") from err

    return {user_stop: value[1] for user_stop, value in candidates.items()}


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse a BISON ISO timestamp, tolerating >6 fractional digits."""
    if not value:
        return None

    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"

    match = re.match(
        r"^(?P<head>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
        r"(?:\.(?P<fraction>\d+))?(?P<tz>Z|[+-]\d{2}:\d{2})?$",
        text,
    )
    if match:
        fraction = (match.group("fraction") or "")[:6]
        timezone = match.group("tz") or ""
        text = match.group("head")
        if fraction:
            text += f".{fraction}"
        if timezone == "Z":
            timezone = "+00:00"
        text += timezone

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=AMSTERDAM)
    return parsed


_KV6_EVENT_TYPES = frozenset(
    {
        "ARRIVAL",
        "DELAY",
        "DEPARTURE",
        "END",
        "INIT",
        "OFFROUTE",
        "ONROUTE",
        "ONSTOP",
    }
)


def parse_kv6_xml(
    xml_bytes: bytes,
    *,
    data_owner: str | None = None,
    line_planning_number: str | None = None,
    received_at: datetime | None = None,
) -> list[Kv6Event]:
    """Parse one KV6 PUSH and only materialize events relevant to this tracker.

    The NDOV topic contains every Arriva vehicle. Filtering while iterating the
    XML avoids retaining a complete tree and constructing Python objects for
    unrelated lines.
    """
    events: list[Kv6Event] = []

    try:
        for _, element in ET.iterparse(BytesIO(xml_bytes), events=("end",)):
            event_type = _local_name(element.tag).upper()
            if event_type not in _KV6_EVENT_TYPES:
                continue

            owner = _child_text(element, "dataownercode")
            line = _child_text(element, "lineplanningnumber")
            if data_owner is not None and owner != data_owner:
                element.clear()
                continue
            if line_planning_number is not None and line != line_planning_number:
                element.clear()
                continue

            source = _child_text(element, "source")
            events.append(
                Kv6Event(
                    event_type=event_type,
                    data_owner_code=owner,
                    line_planning_number=line,
                    operating_day=_child_text(element, "operatingday"),
                    journey_number=_as_int(_child_text(element, "journeynumber")),
                    reinforcement_number=_as_int(_child_text(element, "reinforcementnumber")),
                    user_stop_code=_child_text(element, "userstopcode"),
                    passage_sequence_number=_as_int(_child_text(element, "passagesequencenumber")),
                    timestamp=parse_timestamp(_child_text(element, "timestamp")),
                    source=source.upper() if source else None,
                    punctuality=_as_int(_child_text(element, "punctuality")),
                    vehicle_number=_child_text(element, "vehiclenumber"),
                    received_at=received_at,
                )
            )
            element.clear()
    except ET.ParseError as err:
        raise ParseError(f"Invalid KV6 XML: {err}") from err

    return events


def parse_drgl_route(html: str, target: str) -> tuple[str, ...]:
    """Read ordered stop links from the journey calls panel, ending at target."""
    panel = html.split('id="ott-main-journeycalls"', 1)
    if len(panel) != 2:
        return ()
    stops = []
    for code in re.findall(r"href=[\"\']/stop/(NL:S:[^\"\'/?#]+)", panel[1]):
        if not stops or stops[-1] != code:
            stops.append(code)
        if code == target:
            return tuple(stops) if len(stops) > 1 else ()
    return ()


def parse_drgl_dwell_calls(html: str, operating_day: str) -> tuple[ScheduledDwell, ...]:
    """Read explicit planned arrival/departure differences, ignoring +/- updates."""
    from datetime import timedelta

    panel = html.split('id="ott-main-journeycalls"', 1)
    if len(panel) != 2:
        return ()
    day = date.fromisoformat(operating_day)
    previous = None
    result = []
    for match in re.finditer(
        r'<a\b[^>]*href=["\']/stop/(?P<code>NL:S:[^"\'/?#]+)["\'][^>]*>(?P<body>.*?)</a>',
        panel[1],
        re.IGNORECASE | re.DOTALL,
    ):
        times = {}
        for kind in ("arrival", "departure"):
            value = re.search(
                rf'class=["\'][^"\']*\bott-call-{kind}time\b[^"\']*["\'][^>]*>(.*?)</span>',
                match["body"],
                re.IGNORECASE | re.DOTALL,
            )
            clock = _CLOCK_RE.search(_clean_html(value[1])) if value else None
            if clock:
                parsed = datetime.combine(day, datetime.min.time(), AMSTERDAM).replace(
                    hour=int(clock["hour"]),
                    minute=int(clock["minute"]),
                )
                if previous is not None and parsed < previous - timedelta(hours=12):
                    parsed += timedelta(days=1)
                    day = parsed.date()
                times[kind] = parsed
                previous = parsed
        if times.keys() >= {"arrival", "departure"} and times["departure"] > times["arrival"]:
            result.append(ScheduledDwell(match["code"], times["arrival"], times["departure"]))
    return tuple(result)
