"""Build a small route/stop catalogue outside HA from OVapi GTFS and official CHB.

No timetable, trip times, route shapes or vehicle data are shipped to HA.
GTFS realtime_trip_id provides the authoritative ARR line planning number.
Stop matching requires an exact normalized town/name and a unique active CHB
StopPlace within 30 metres of a GTFS boarding point; ambiguous matches are omitted.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import re
import tempfile
import unicodedata
import urllib.request
import zipfile
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

GTFS_URL = "https://gtfs.ovapi.nl/nl/gtfs-nl.zip"
CHB_INDEX = "https://data.ndovloket.nl/haltes/"
USER_AGENT = (
    "HomeAssistant-ArrivaBus-Catalog (github.com/digital-IMEI/home-assistant-bus-57-bovenstraat)"
)
NS = "{http://bison.connekt.nl/tmi8/chb/msg}"


def normalized(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def ordered_stops(patterns):
    """Use the longest actual trip as backbone, insert branches at shared stops.

    Alternative routes cannot have one universal chronology. Preserve the main
    trip and insert each branch before its next shared stop, never alphabetically.
    """
    counts = Counter(tuple(dict.fromkeys(pattern)) for pattern in patterns if pattern)
    result = []
    for pattern in sorted(counts, key=lambda p: (-len(p), -counts[p], p)):
        for index, code in enumerate(pattern):
            if code in result:
                continue
            following = next((c for c in pattern[index + 1 :] if c in result), None)
            if following is None:
                result.append(code)
            else:
                result.insert(result.index(following), code)
    return result


def active(element, day):
    start = element.findtext(NS + "validfrom")
    end = element.findtext(NS + "validuntil")
    return (not start or start[:10] <= day.isoformat()) and (not end or day.isoformat() < end[:10])


def latest(elements, day):
    eligible = [item for item in elements if active(item, day)]
    return max(eligible, key=lambda x: x.findtext(NS + "validfrom") or "", default=None)


def chb_stops(path, day):
    """Read only active named stop places and boarding point coordinates."""
    result = defaultdict(list)
    with gzip.open(path, "rb") as stream:
        for _, place in ET.iterparse(stream, events=("end",)):
            if place.tag != NS + "stopplace":
                continue
            if active(place, day):
                name = latest(place.findall(NS + "stopplacename"), day)
                code = place.findtext(NS + "stopplacecode")
                if name is not None and code and code.startswith("NL:S:"):
                    key = normalized(
                        f"{name.findtext(NS + 'town')}, {name.findtext(NS + 'publicname')}"
                    )
                    for quay in place.findall(f"{NS}quays/{NS}quay"):
                        if not active(quay, day):
                            continue
                        status = latest(quay.findall(NS + "quaystatusdata"), day)
                        if status is not None and status.findtext(NS + "quaystatus") != "available":
                            continue
                        loc = latest(quay.findall(NS + "quaylocationdata"), day)
                        if loc is not None:
                            try:
                                xy = (
                                    float(loc.findtext(NS + "rd-x")),
                                    float(loc.findtext(NS + "rd-y")),
                                )
                            except TypeError, ValueError:
                                continue
                            result[key].append((code, *xy))
            place.clear()
    return result


def build_catalog(gtfs, chb, today):
    from pyproj import Transformer  # Build dependency only; never loaded in Home Assistant.

    projection = Transformer.from_crs(4326, 28992, always_xy=True)
    chb_by_name = chb_stops(chb, today)
    with zipfile.ZipFile(gtfs) as archive:

        def rows(filename):
            return csv.DictReader(io.TextIOWrapper(archive.open(filename), encoding="utf-8-sig"))

        feed = next(rows("feed_info.txt"))
        end = min(today + timedelta(days=28), date.fromisoformat(feed["feed_end_date"]))
        if end < today:
            raise ValueError("GTFS feed has expired; retaining previous catalogue")
        services = {
            r["service_id"]
            for r in rows("calendar_dates.txt")
            if r["exception_type"] == "1"
            and today.strftime("%Y%m%d") <= r["date"] <= end.strftime("%Y%m%d")
        }
        routes = {
            r["route_id"]: r
            for r in rows("routes.txt")
            if r["route_type"].isdigit()
            and (int(r["route_type"]) == 3 or 700 <= int(r["route_type"]) < 800)
        }
        lines = {}
        trips = {}
        for row in rows("trips.txt"):
            match = re.fullmatch(r"ARR:([A-Za-z0-9_-]+):\d+", row["realtime_trip_id"])
            if not match or row["route_id"] not in routes or row["service_id"] not in services:
                continue
            route = routes[row["route_id"]]
            planning = match[1]
            headsign = row["trip_headsign"].strip()
            if not headsign:
                continue
            line = lines.setdefault(
                planning,
                {
                    "id": planning,
                    "number": route["route_short_name"],
                    "name": route["route_long_name"],
                    "directions": {},
                },
            )
            if line["number"] != route["route_short_name"]:
                raise ValueError(f"Conflicting public numbers for ARR:{planning}")
            # GTFS direction_id groups short workings/alternative termini in
            # the same direction; missing IDs remain separate rather than guessed.
            direction = row.get("direction_id", "").strip()
            group = direction if direction in {"0", "1"} else headsign
            line["directions"].setdefault(group, {"names": set(), "stops": set()})["names"].add(
                headsign
            )
            trips[row["trip_id"]] = (planning, group, headsign)
        stops = {r["stop_id"]: r for r in rows("stops.txt") if r["location_type"] in ("", "0")}
        mapped = {}
        for sid, stop in stops.items():
            key = normalized(stop["stop_name"])
            candidates = chb_by_name.get(key, ())
            if not candidates:
                continue
            x, y = projection.transform(float(stop["stop_lon"]), float(stop["stop_lat"]))
            matches = {
                code for code, cx, cy in candidates if (x - cx) ** 2 + (y - cy) ** 2 <= 30**2
            }
            if len(matches) == 1:
                mapped[sid] = matches.pop()
        aliases = defaultdict(lambda: defaultdict(set))
        trip_calls = defaultdict(list)
        used, matched = set(), set()
        names = {}
        for row in rows("stop_times.txt"):
            trip = trips.get(row["trip_id"])
            if trip is None:
                continue
            sid = row["stop_id"]
            used.add(sid)
            code = mapped.get(sid)
            if code is None:
                continue
            matched.add(sid)
            planning, group, headsign = trip
            # A stop-specific headsign is the displayed destination at this stop.
            destination = row.get("stop_headsign", "").strip() or headsign
            lines[planning]["directions"][group]["stops"].add(code)
            aliases[(planning, group)][code].update((headsign, destination))
            sequence = int(row.get("stop_sequence") or len(trip_calls[row["trip_id"]]))
            trip_calls[row["trip_id"]].append((sequence, code))
            names[code] = stops[sid]["stop_name"]
        result = []
        patterns = defaultdict(list)
        for trip_id, calls in trip_calls.items():
            planning, group, _ = trips[trip_id]
            patterns[(planning, group)].append([code for _, code in sorted(calls)])
        for line in lines.values():
            line["directions"] = [
                {
                    "name": " / ".join(sorted(direction["names"])),
                    "stops": ordered_stops(patterns[(line["id"], group)]),
                    "headsigns": {
                        code: sorted(aliases[(line["id"], group)][code])
                        for code in sorted(direction["stops"])
                    },
                }
                for group, direction in sorted(line["directions"].items())
                if direction["stops"]
            ]
            # Some circular/reserve services have the same headsign in both
            # directions. Keep selectable keys unique; do not drop a direction.
            labels = Counter(direction["name"] for direction in line["directions"])
            for index, direction in enumerate(line["directions"], start=1):
                if labels[direction["name"]] > 1:
                    direction["name"] += f" (richting {index})"
            if line["directions"]:
                result.append(line)
        if not result or not names:
            raise ValueError("No mapped Arriva bus routes; retaining previous catalogue")
        result.sort(
            key=lambda line: (
                int(line["number"]) if line["number"].isdigit() else 99999,
                line["number"],
                line["name"],
            )
        )
        return {
            "schema": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "valid_until": end.isoformat(),
            "feed_version": feed["feed_version"],
            "source": GTFS_URL,
            "coverage": {
                "gtfs_stops": len(used),
                "mapped_gtfs_stops": len(matched),
                "omitted_gtfs_stops": len(used - matched),
            },
            "stops": names,
            "lines": result,
        }


def download(url, path):
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
    )
    with urllib.request.urlopen(request, timeout=180) as response, open(path, "wb") as output:
        source = (
            gzip.GzipFile(fileobj=response)
            if response.headers.get("Content-Encoding") == "gzip"
            else response
        )
        while chunk := source.read(1024 * 1024):
            output.write(chunk)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gtfs", type=Path)
    parser.add_argument("--chb", type=Path)
    parser.add_argument("--date", type=date.fromisoformat, default=datetime.now(UTC).date())
    parser.add_argument(
        "--output", type=Path, default=Path("custom_components/arriva_bus/data/catalog.json.gz")
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gtfs, chb = args.gtfs or root / "gtfs.zip", args.chb or root / "chb.xml.gz"
        if not args.gtfs:
            download(GTFS_URL, gtfs)
        if not args.chb:
            download(CHB_INDEX, root / "index.html")
            names = re.findall(
                r"(?<!Assignment)ExportCHB_(\d{4}-\d{2}-\d{2})\.xml\.gz",
                (root / "index.html").read_text(),
            )
            eligible = [d for d in names if d <= args.date.isoformat()]
            if not eligible:
                raise ValueError("No current CHB export found")
            download(CHB_INDEX + f"ExportCHB_{max(eligible)}.xml.gz", chb)
        catalog = build_catalog(gtfs, chb, args.date)
        payload = json.dumps(catalog, ensure_ascii=False, separators=(",", ":")).encode()
        compressed = gzip.compress(payload, mtime=0)
        if len(compressed) > 2_000_000:
            raise ValueError("Catalogue exceeds the HA size budget")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        staged = args.output.with_suffix(".tmp")
        staged.write_bytes(compressed)
        staged.replace(args.output)
        print(
            json.dumps(
                {
                    "lines": len(catalog["lines"]),
                    "stops": len(catalog["stops"]),
                    "json_bytes": len(payload),
                    "compressed_bytes": len(compressed),
                    **catalog["coverage"],
                }
            )
        )


if __name__ == "__main__":
    main()
