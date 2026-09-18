"""Small HTTP client for timetable, PSA stop mapping and stop names."""

from __future__ import annotations

import asyncio
import gzip
from datetime import date, datetime

from aiohttp import ClientError, ClientSession, ClientTimeout

from .const import (
    DATA_OWNER,
    DRGL_BASE_URL,
    NDOV_HALTES_BASE_URL,
    PSA_INDEX_URL,
    REQUEST_TIMEOUT_SECONDS,
    STOP_NAME_RETRY_INTERVAL,
    USER_AGENT,
)
from .models import (
    AMSTERDAM,
    JourneySelection,
    ParseError,
    ScheduledDwell,
    parse_drgl_departures,
    parse_drgl_dwell_calls,
    parse_drgl_route,
    parse_drgl_stop_name,
    parse_passenger_stop_assignments,
    select_latest_psa_filename,
)
from .route_config import RouteConfig


class TransitHttpError(Exception):
    """HTTP or public-data source failed."""


class TransitHttpClient:
    """Fetch the small HTTP resources used beside the realtime KV6 stream."""

    def __init__(self, session: ClientSession, route: RouteConfig | None = None) -> None:
        self.route = route
        self._dwell_calls: dict[str, tuple[ScheduledDwell, ...]] = {}
        self._session = session
        self._timeout = ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
        self._headers = {
            "User-Agent": USER_AGENT,
            "Cache-Control": "no-cache",
        }
        self._stop_name_cache: dict[str, str] = {}
        self._stop_name_retry_after: dict[str, datetime] = {}
        self._psa_mapping: dict[str, str] | None = None
        self._psa_loaded_for: date | None = None
        self._psa_filename: str | None = None

    async def _get_text_url(self, url: str) -> str:
        try:
            async with self._session.get(
                url,
                headers={**self._headers, "Accept": "text/html,application/xhtml+xml"},
                timeout=self._timeout,
            ) as response:
                response.raise_for_status()
                return await response.text()
        except (ClientError, TimeoutError, UnicodeError) as err:
            raise TransitHttpError(f"GET {url} failed: {err}") from err

    async def _get_bytes_url(self, url: str) -> bytes:
        try:
            async with self._session.get(
                url,
                headers={
                    **self._headers,
                    "Accept": "application/gzip,application/octet-stream,*/*",
                },
                timeout=self._timeout,
            ) as response:
                response.raise_for_status()
                return await response.read()
        except (ClientError, TimeoutError) as err:
            raise TransitHttpError(f"GET {url} failed: {err}") from err

    async def async_get_journeys(self) -> list[JourneySelection]:
        """Return journeys for the configured stop, line and destination."""
        assert self.route is not None
        html = await self._get_text_url(f"{DRGL_BASE_URL}/stop/{self.route.target_stop_code}")
        return parse_drgl_departures(
            html,
            destination=self.route.destination,
            destination_aliases=self.route.destination_aliases,
            line=self.route.line_public_number,
            line_planning_number=self.route.line_planning_number,
        )

    async def async_get_route(self, journey: JourneySelection) -> tuple[str, ...]:
        """Fetch the actual stop order for this particular journey."""
        html = await self._get_text_url(f"{DRGL_BASE_URL}/journey/{journey.key}/")
        assert self.route is not None
        self._dwell_calls = {journey.key: parse_drgl_dwell_calls(html, journey.operating_day)}
        return parse_drgl_route(html, self.route.target_stop_code)

    def get_dwell_calls(self, journey: JourneySelection) -> tuple[ScheduledDwell, ...]:
        return self._dwell_calls.get(journey.key, ())

    async def async_get_passenger_stop_assignments(self) -> dict[str, str]:
        """Return today's official ARR UserStopCode -> CHB StopPlaceCode map.

        The NDOV PassengerStopAssignment export is about 1 MB compressed and is
        downloaded at most once per local calendar day per HA process. Parsing
        and gzip decompression run off the Home Assistant event loop.
        """
        today = datetime.now(AMSTERDAM).date()
        if self._psa_mapping is not None and self._psa_loaded_for == today:
            return self._psa_mapping

        try:
            index_html = await self._get_text_url(PSA_INDEX_URL)
            filename = select_latest_psa_filename(index_html, today)
            if not filename:
                raise TransitHttpError("No PassengerStopAssignment export found in NDOV index")

            payload = await self._get_bytes_url(f"{NDOV_HALTES_BASE_URL}/{filename}")

            def decode_and_parse() -> dict[str, str]:
                # A .gz file normally arrives as gzip bytes. Be tolerant of an
                # HTTP intermediary that has already decoded Content-Encoding.
                xml_bytes = gzip.decompress(payload) if payload[:2] == b"\x1f\x8b" else payload
                return parse_passenger_stop_assignments(
                    xml_bytes,
                    data_owner=DATA_OWNER,
                    valid_on=today,
                )

            mapping = await asyncio.to_thread(decode_and_parse)
            if not mapping:
                raise TransitHttpError(
                    f"PassengerStopAssignment {filename} contains no active {DATA_OWNER} mappings"
                )

        except (OSError, ParseError) as err:
            # Keep a previously working map if a daily refresh is temporarily bad.
            if self._psa_mapping is not None:
                return self._psa_mapping
            raise TransitHttpError(f"PassengerStopAssignment parsing failed: {err}") from err
        except TransitHttpError:
            if self._psa_mapping is not None:
                return self._psa_mapping
            raise

        self._psa_mapping = mapping
        self._psa_loaded_for = today
        self._psa_filename = filename
        return mapping

    @property
    def psa_filename(self) -> str | None:
        """Return the currently loaded PSA export filename for diagnostics."""
        return self._psa_filename

    @property
    def psa_loaded_for(self) -> date | None:
        """Return the service date represented by the cached PSA mapping."""
        return self._psa_loaded_for

    @property
    def psa_is_stale(self) -> bool:
        """Return whether a retained mapping predates the current service day."""
        return (
            self._psa_mapping is not None and self._psa_loaded_for != datetime.now(AMSTERDAM).date()
        )

    def get_cached_stop_name(self, stop_place_code: str) -> str | None:
        """Return a previously resolved stop name without doing I/O."""
        return self._stop_name_cache.get(stop_place_code)

    def stop_name_resolution_due(self, stop_place_code: str) -> bool:
        """Return whether an uncached stop name may be requested now."""
        if stop_place_code in self._stop_name_cache:
            return False
        retry_after = self._stop_name_retry_after.get(stop_place_code)
        return retry_after is None or datetime.now(AMSTERDAM) >= retry_after

    async def async_get_stop_name(self, stop_place_code: str) -> str | None:
        """Resolve a NATIONAL CHB StopPlaceCode to a human-readable name.

        This method deliberately does not accept/fallback to a KV6 UserStopCode.
        Those codes live in the operator's own domain and first have to be
        normalized through PassengerStopAssignment.
        """
        if name := self._stop_name_cache.get(stop_place_code):
            return name

        now = datetime.now(AMSTERDAM)
        if not self.stop_name_resolution_due(stop_place_code):
            return None

        try:
            # PassengerStopAssignment already returns the complete national
            # identifier (for example ``NL:S:66420180``).
            html = await self._get_text_url(f"{DRGL_BASE_URL}/stop/{stop_place_code}")
        except TransitHttpError:
            self._stop_name_retry_after[stop_place_code] = now + STOP_NAME_RETRY_INTERVAL
            return None

        name = parse_drgl_stop_name(html)
        if name:
            self._stop_name_cache[stop_place_code] = name
            self._stop_name_retry_after.pop(stop_place_code, None)
        else:
            self._stop_name_retry_after[stop_place_code] = now + STOP_NAME_RETRY_INTERVAL
        return name
