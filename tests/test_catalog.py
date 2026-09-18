"""Catalogue transport, mapping ambiguity and service-day filtering."""

import gzip
import json
import zipfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pyproj import Transformer

from custom_components.arriva_bus.api import TransitHttpError
from custom_components.arriva_bus.catalog import (
    CatalogError,
    RouteCatalog,
    async_load_catalog,
    decode_catalog,
)
from scripts.build_arriva_catalog import build_catalog, ordered_stops


def catalog_data():
    return {
        "schema": 1,
        "feed_version": "test",
        "valid_until": "2099-01-01",
        "stops": {"NL:S:one": "Town, Stop"},
        "lines": [
            {
                "id": "26057",
                "number": "57",
                "name": "A - B",
                "directions": [{"name": "B", "stops": ["NL:S:one"]}],
            }
        ],
    }


def test_stop_order_follows_trips_and_inserts_branches_not_alphabet():
    assert ordered_stops([("z", "b", "c", "a"), ("b", "extra", "c")]) == [
        "z",
        "b",
        "extra",
        "c",
        "a",
    ]
    assert ordered_stops([("z", "b", "z", "a")]) == ["z", "b", "a"]


def test_all_catalogue_directions_are_unique_and_line57_is_in_route_order():
    data = json.loads(
        gzip.decompress(Path("custom_components/arriva_bus/data/catalog.json.gz").read_bytes())
    )
    for line in data["lines"]:
        assert len({direction["name"] for direction in line["directions"]}) == len(
            line["directions"]
        )
        assert len(line["directions"]) <= 2
    catalog = RouteCatalog(data, today=date(2026, 9, 17))
    names = list(catalog.stops("26057", "Maastricht").values())
    assert names[0] == "Gulpen, Busstation"
    assert names[-1] == "Maastricht, Station Maastricht"
    assert names.index("Gulpen, Oude Geul") < names.index("Noorbeek, Bovenstraat")
    assert names.index("Noorbeek, Bovenstraat") < names.index("Maastricht, Station Maastricht")


def test_catalog_validates_expiry_and_references():
    data = catalog_data()
    assert RouteCatalog(data).line_choices() == {"26057": "57 · A - B"}
    data["valid_until"] = "2020-01-01"
    with pytest.raises(CatalogError):
        RouteCatalog(data)
    data = catalog_data()
    data["lines"][0]["directions"][0]["stops"] = ["NL:S:missing"]
    with pytest.raises(CatalogError):
        RouteCatalog(data)
    with pytest.raises(CatalogError):
        decode_catalog(b"invalid gzip")


@pytest.mark.asyncio
async def test_offline_bundled_catalogue_and_shared_cache(tmp_path):
    asset = tmp_path / "catalog.json.gz"
    asset.write_bytes(gzip.compress(json.dumps(catalog_data()).encode()))

    async def executor(fn, *args):
        return fn(*args)

    hass = SimpleNamespace(data={}, async_add_executor_job=executor)
    with (
        patch("custom_components.arriva_bus.catalog.async_get_clientsession"),
        patch("custom_components.arriva_bus.catalog.BUNDLED_CATALOG", asset),
        patch(
            "custom_components.arriva_bus.catalog.TransitHttpClient._get_bytes_url",
            new_callable=AsyncMock,
            side_effect=TransitHttpError("offline"),
        ) as fetch,
    ):
        first = await async_load_catalog(hass)
        second = await async_load_catalog(hass)
    assert first is second
    fetch.assert_awaited_once()


def test_prebuilt_catalogue_is_small_and_contains_original_stop():
    path = Path("custom_components/arriva_bus/data/catalog.json.gz")
    assert path.stat().st_size < 2_000_000
    data = json.loads(gzip.decompress(path.read_bytes()))
    catalog = RouteCatalog(data, today=date(2026, 9, 17))
    assert "NL:S:66420180" in catalog.stops("26057", "Maastricht")
    assert catalog.public_number("26350") == "350"


@pytest.mark.parametrize("merge", [False, True])
def test_builder_filters_services_trains_operators_and_ambiguous_stops(tmp_path, merge):
    lat, lon = 50.770857, 5.816149
    x, y = Transformer.from_crs(4326, 28992, always_xy=True).transform(lon, lat)
    ns = "http://bison.connekt.nl/tmi8/chb/msg"

    def place(code, name):
        return f"""<stopplace><stopplacecode>{code}</stopplacecode><validfrom>2020-01-01</validfrom>
        <stopplacename><validfrom>2020-01-01</validfrom><town>Town</town><publicname>{name}</publicname></stopplacename>
        <quays><quay><validfrom>2020-01-01</validfrom><quaylocationdata><rd-x>{x}</rd-x><rd-y>{y}</rd-y></quaylocationdata></quay></quays></stopplace>"""

    chb = tmp_path / "chb.xml.gz"
    chb.write_bytes(
        gzip.compress(
            (
                f'<export xmlns="{ns}"><stopplaces>'
                + place("NL:S:one", "Stop")
                + place("NL:S:two", "Ambiguous")
                + place("NL:S:three", "Ambiguous")
                + "</stopplaces></export>"
            ).encode()
        )
    )
    gtfs = tmp_path / "gtfs.zip"
    files = {
        "feed_info.txt": "feed_version,feed_end_date\ntest,20261212\n",
        "calendar_dates.txt": "service_id,date,exception_type\ncurrent,20260918,1\nexpired,20260916,1\ncancelled,20260918,2\n",
        "routes.txt": "route_id,route_type,route_short_name,route_long_name\nbus,3,57,A - B\ntrain,2,RS1,Train\n",
        "trips.txt": "trip_id,route_id,service_id,realtime_trip_id,trip_headsign\nt1,bus,current,ARR:26057:1,B\nt2,bus,expired,ARR:26058:2,Expired\nt3,bus,current,CXX:26057:3,Other\nt4,train,current,ARR:26059:4,Train\nt5,bus,cancelled,ARR:26060:5,Cancelled\n",
        "stops.txt": f'stop_id,stop_name,stop_lat,stop_lon,location_type\ns1,"Town, Stop",{lat},{lon},0\ns2,"Town, Ambiguous",{lat},{lon},0\n',
        "stop_times.txt": "trip_id,stop_id,stop_headsign\nt1,s1,B Station\nt1,s2,\nt2,s1,\nt3,s1,\nt4,s1,\nt5,s1,\n",
    }
    if merge:
        rows = files["trips.txt"].splitlines()
        files["trips.txt"] = (
            rows[0]
            + ",direction_id\n"
            + "\n".join(row + ",0" for row in rows[1:])
            + "\nt6,bus,current,ARR:26057:6,C,0\n"
        )
        files["stop_times.txt"] += "t6,s1,\n"
    with zipfile.ZipFile(gtfs, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)
    result = build_catalog(gtfs, chb, date(2026, 9, 17))
    assert [line["id"] for line in result["lines"]] == ["26057"]
    assert result["stops"] == {"NL:S:one": "Town, Stop"}
    assert result["coverage"]["omitted_gtfs_stops"] == 1
    assert [d["name"] for d in result["lines"][0]["directions"]] == ["B / C" if merge else "B"]
    assert result["lines"][0]["directions"][0]["headsigns"] == {
        "NL:S:one": ["B", "B Station", "C"] if merge else ["B", "B Station"]
    }


def test_stop_headsign_aliases_are_scoped_to_the_stop_and_direction():
    data = catalog_data()
    data["lines"][0]["directions"] = [
        {"name": "B", "stops": ["NL:S:one"], "headsigns": {"NL:S:one": ["B Station"]}},
        {"name": "A", "stops": ["NL:S:one"]},
    ]
    catalog = RouteCatalog(data)
    assert catalog.aliases("26057", "B Station", "NL:S:one") == ("B", "B Station")
    assert catalog.aliases("26057", "B", "NL:S:one") == ("B", "B Station")
    assert catalog.aliases("26057", "A", "NL:S:one") == ("A",)
    assert catalog.aliases("26057", "B", "NL:S:other") == ("B",)


def test_line57_maastricht_includes_city_and_outlying_stops():
    data = json.loads(
        gzip.decompress(Path("custom_components/arriva_bus/data/catalog.json.gz").read_bytes())
    )
    catalog = RouteCatalog(data, today=date(2026, 9, 17))
    assert set(catalog.directions("26057")) == {"Gulpen", "Maastricht"}
    stops = catalog.stops("26057", "Maastricht")
    assert "NL:S:66420180" in stops
    city = [code for code, name in stops.items() if name.startswith("Maastricht,")]
    assert len(city) >= 10
    assert any(
        "Maastricht Station" in catalog.aliases("26057", "Maastricht", code) for code in city
    )
