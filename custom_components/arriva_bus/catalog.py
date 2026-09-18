"""Load only the prebuilt compact catalogue; GTFS processing never runs in HA."""

from __future__ import annotations

import asyncio
import gzip
import json
from datetime import date, datetime
from pathlib import Path
from time import monotonic

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import TransitHttpClient, TransitHttpError
from .const import DOMAIN
from .models import AMSTERDAM

CATALOG_URL = (
    "https://raw.githubusercontent.com/digital-IMEI/"
    "home-assistant-arriva-bus/main/"
    "custom_components/arriva_bus/data/catalog.json.gz"
)
BUNDLED_CATALOG = Path(__file__).parent / "data" / "catalog.json.gz"
MAX_CATALOG_BYTES = 8_000_000


class CatalogError(ValueError):
    """No valid, unexpired compact catalogue could be loaded."""


class RouteCatalog:
    """Validated line/destination/stop options, independent of live departures."""

    def __init__(self, data: dict, today: date | None = None) -> None:
        today = today or datetime.now(AMSTERDAM).date()
        try:
            if data["schema"] != 1 or date.fromisoformat(data["valid_until"]) < today:
                raise CatalogError("Catalogue schema is unsupported or catalogue has expired")
            self.valid_until = data["valid_until"]
            self.feed_version = data["feed_version"]
            self._stops = data["stops"]
            self._lines = {line["id"]: line for line in data["lines"]}
            if not self._lines or not self._stops:
                raise CatalogError("Empty catalogue")
            for line in self._lines.values():
                if not line["number"] or not line["directions"]:
                    raise CatalogError("Incomplete line")
                for direction in line["directions"]:
                    if not direction["name"] or not direction["stops"]:
                        raise CatalogError("Incomplete destination")
                    for code in direction["stops"]:
                        if not code.startswith("NL:S:") or code not in self._stops:
                            raise CatalogError("Invalid stop reference")
        except (KeyError, TypeError, ValueError) as err:
            raise CatalogError(str(err)) from err

    def line_choices(self) -> dict[str, str]:
        return {code: f"{line['number']} · {line['name']}" for code, line in self._lines.items()}

    def public_number(self, planning: str) -> str:
        return self._lines[planning]["number"]

    def directions(self, planning: str) -> dict[str, str]:
        return {item["name"]: item["name"] for item in self._lines[planning]["directions"]}

    def aliases(self, planning: str, destination: str, stop: str) -> tuple[str, ...]:
        """Resolve known names at this stop without merging opposite directions."""
        matches = []
        for direction in self._lines.get(planning, {}).get("directions", []):
            if stop not in direction["stops"]:
                continue
            names = {direction["name"], *direction.get("headsigns", {}).get(stop, [])}
            if destination.casefold() in {name.casefold() for name in names}:
                matches.append(names)
        return tuple(sorted(matches[0])) if len(matches) == 1 else (destination,)

    def stops(self, planning: str, destination: str) -> dict[str, str]:
        direction = next(
            item for item in self._lines[planning]["directions"] if item["name"] == destination
        )
        return {code: self._stops[code] for code in direction["stops"]}


def decode_catalog(payload: bytes) -> RouteCatalog:
    import io

    try:
        with gzip.GzipFile(fileobj=io.BytesIO(payload)) as stream:
            content = stream.read(MAX_CATALOG_BYTES + 1)
        if len(content) > MAX_CATALOG_BYTES:
            raise CatalogError("Catalogue exceeds size budget")
        return RouteCatalog(json.loads(content))
    except (OSError, EOFError, ValueError, TypeError) as err:
        raise CatalogError(str(err)) from err


async def async_load_catalog(hass) -> RouteCatalog:
    """Check at most daily during onboarding; use bundled data if offline."""
    shared = hass.data.setdefault(DOMAIN, {})
    lock = shared.setdefault("catalog_lock", asyncio.Lock())
    async with lock:
        previous = shared.get("catalog")
        now = monotonic()
        if (
            previous
            and now - shared["catalog_checked_at"] < 86400
            and previous.valid_until >= datetime.now(AMSTERDAM).date().isoformat()
        ):
            return previous
        try:
            payload = await TransitHttpClient(async_get_clientsession(hass))._get_bytes_url(
                CATALOG_URL
            )
            catalog = await hass.async_add_executor_job(decode_catalog, payload)
        except TransitHttpError, CatalogError:
            if previous and previous.valid_until >= datetime.now(AMSTERDAM).date().isoformat():
                return previous
            try:
                payload = await hass.async_add_executor_job(BUNDLED_CATALOG.read_bytes)
                catalog = await hass.async_add_executor_job(decode_catalog, payload)
            except (OSError, CatalogError) as err:
                raise CatalogError(
                    "No valid catalogue available; retry or update the integration"
                ) from err
        shared["catalog"], shared["catalog_checked_at"] = catalog, now
        return catalog
