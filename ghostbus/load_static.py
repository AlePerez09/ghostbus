"""Load a static GTFS feed (zip URL or local path) into the relational tables.

Usage:
    python -m ghostbus.load_static                 # downloads every agency in config.AGENCIES
    python -m ghostbus.load_static path/to/gtfs.zip   # one local feed, loaded as Miami-Dade
"""
import csv
import io
import sys
import zipfile
from pathlib import Path

import psycopg
import requests

from .config import AGENCIES, DATABASE_URL, ROUTE_LABEL


def gtfs_time(s: str):
    """'25:10:00' -> 90600 seconds after service-day midnight."""
    if not s:
        return None
    h, m, sec = s.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + int(sec)


def gtfs_date(s: str):
    s = s.strip()
    return f"{s[:4]}-{s[4:6]}-{s[6:]}"


def rows(z: zipfile.ZipFile, name: str):
    if name not in z.namelist():
        return
    with z.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))


def copy(cur, table, cols, data):
    n = 0
    with cur.copy(f"COPY {table} ({', '.join(cols)}) FROM STDIN") as cp:
        for r in data:
            cp.write_row(r)
            n += 1
    print(f"  {table}: {n:,} rows")


def open_zip(src: str) -> zipfile.ZipFile:
    if Path(src).exists():
        return zipfile.ZipFile(src)
    print(f"Downloading {src} ...")
    resp = requests.get(src, timeout=120)
    resp.raise_for_status()
    return zipfile.ZipFile(io.BytesIO(resp.content))


def main():
    if len(sys.argv) > 1:
        load(sys.argv[1])
    else:
        loaded, failed = load_all(require_all=False)
        if failed:
            print("Skipped (download failed): " + ", ".join(failed))


def load(src: str):
    """Load a single feed as Miami-Dade (used for local files and the built-in sample)."""
    load_feeds([("mdt", "", open_zip(src))])


def load_all(require_all=False):
    """Download every agency's schedule, then load them together. Returns (loaded_keys, failed_keys).
    Downloads happen before anything is deleted, so a dead county website never empties the database.
    require_all=True (weekly refresh) keeps the current schedule unless every agency downloaded."""
    feeds, failed = [], []
    for a in AGENCIES:
        try:
            feeds.append((a["key"], a["prefix"], open_zip(a["static_url"])))
        except Exception as ex:
            print(f"  {a['name']}: download failed ({ex})")
            failed.append(a["key"])
    if not feeds or (require_all and failed):
        raise RuntimeError("schedule download failed for: " + ", ".join(failed))
    load_feeds(feeds)
    return [k for k, _, _ in feeds], failed


def load_feeds(feeds):
    """feeds: list of (agency_key, id_prefix, ZipFile). Everything loads in one transaction."""
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE routes, stops, trips, stop_times, calendar, calendar_dates, shapes, trip_windows")
        for agency, prefix, z in feeds:
            print(f"Loading {agency} ...")
            load_one(cur, agency, prefix, z)
        cur.execute("""
            INSERT INTO trip_windows (trip_id, route_id, service_id, direction_id, start_s, end_s, agency)
            SELECT t.trip_id, t.route_id, t.service_id, t.direction_id, min(st.arrival_s), max(st.arrival_s), t.agency
            FROM trips t JOIN stop_times st USING (trip_id)
            GROUP BY t.trip_id, t.route_id, t.service_id, t.direction_id, t.agency
        """)
        print(f"  trip_windows: {cur.rowcount:,} rows")
        cur.execute("ANALYZE")
    print("Static GTFS loaded.")


def load_one(cur, agency, prefix, z):
    p = lambda v: f"{prefix}{v}" if v not in (None, "") else v      # namespace an ID
    label = ROUTE_LABEL.get(agency, "")

    def short_name(r):
        if not label:
            return r.get("route_short_name")                 # Miami-Dade: unchanged
        return f"{label} {r.get('route_short_name') or r.get('route_long_name') or r['route_id']}"

    copy(cur, "routes", ["route_id", "route_short_name", "route_long_name", "route_type", "route_color", "agency"],
         ((p(r["route_id"]), short_name(r), r.get("route_long_name"),
           int(r.get("route_type") or 3), r.get("route_color"), agency) for r in rows(z, "routes.txt")))
    copy(cur, "stops", ["stop_id", "stop_name", "lat", "lon"],
         ((p(r["stop_id"]), r.get("stop_name"), float(r["stop_lat"]), float(r["stop_lon"]))
          for r in rows(z, "stops.txt") if r.get("stop_lat")))
    copy(cur, "trips", ["trip_id", "route_id", "service_id", "direction_id", "headsign", "shape_id", "agency"],
         ((p(r["trip_id"]), p(r["route_id"]), p(r["service_id"]),
           int(r["direction_id"]) if r.get("direction_id") not in (None, "") else 0,
           r.get("trip_headsign"), p(r.get("shape_id")), agency) for r in rows(z, "trips.txt")))

    def st():
        for r in rows(z, "stop_times.txt"):
            a = gtfs_time(r.get("arrival_time", "")) or gtfs_time(r.get("departure_time", ""))
            d = gtfs_time(r.get("departure_time", "")) or a
            if a is None:
                continue  # untimed stop; skip (interpolation not needed for this app)
            yield (p(r["trip_id"]), int(r["stop_sequence"]), p(r["stop_id"]), a, d)
    copy(cur, "stop_times", ["trip_id", "stop_sequence", "stop_id", "arrival_s", "departure_s"], st())

    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    copy(cur, "calendar", ["service_id", *days, "start_date", "end_date"],
         ((p(r["service_id"]), *[r[d] == "1" for d in days], gtfs_date(r["start_date"]), gtfs_date(r["end_date"]))
          for r in rows(z, "calendar.txt")))
    copy(cur, "calendar_dates", ["service_id", "date", "exception_type"],
         ((p(r["service_id"]), gtfs_date(r["date"]), int(r["exception_type"])) for r in rows(z, "calendar_dates.txt")))
    copy(cur, "shapes", ["shape_id", "seq", "lat", "lon"],
         ((p(r["shape_id"]), int(r["shape_pt_sequence"]), float(r["shape_pt_lat"]), float(r["shape_pt_lon"]))
          for r in rows(z, "shapes.txt")))


if __name__ == "__main__":
    main()
