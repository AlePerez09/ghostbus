"""Load a static GTFS feed (zip URL or local path) into the relational tables.

Usage:
    python -m ghostbus.load_static                 # downloads Miami-Dade's feed
    python -m ghostbus.load_static path/to/gtfs.zip
"""
import csv
import io
import sys
import zipfile
from pathlib import Path

import psycopg
import requests

from .config import DATABASE_URL, GTFS_STATIC_URL


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
    src = sys.argv[1] if len(sys.argv) > 1 else GTFS_STATIC_URL
    z = open_zip(src)
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE routes, stops, trips, stop_times, calendar, calendar_dates, shapes, trip_windows")

        copy(cur, "routes", ["route_id", "route_short_name", "route_long_name", "route_type", "route_color"],
             ((r["route_id"], r.get("route_short_name"), r.get("route_long_name"),
               int(r.get("route_type") or 3), r.get("route_color")) for r in rows(z, "routes.txt")))
        copy(cur, "stops", ["stop_id", "stop_name", "lat", "lon"],
             ((r["stop_id"], r.get("stop_name"), float(r["stop_lat"]), float(r["stop_lon"]))
              for r in rows(z, "stops.txt") if r.get("stop_lat")))
        copy(cur, "trips", ["trip_id", "route_id", "service_id", "direction_id", "headsign", "shape_id"],
             ((r["trip_id"], r["route_id"], r["service_id"],
               int(r["direction_id"]) if r.get("direction_id") not in (None, "") else 0,
               r.get("trip_headsign"), r.get("shape_id")) for r in rows(z, "trips.txt")))

        def st():
            for r in rows(z, "stop_times.txt"):
                a = gtfs_time(r.get("arrival_time", "")) or gtfs_time(r.get("departure_time", ""))
                d = gtfs_time(r.get("departure_time", "")) or a
                if a is None:
                    continue  # untimed stop; skip (interpolation not needed for this app)
                yield (r["trip_id"], int(r["stop_sequence"]), r["stop_id"], a, d)
        copy(cur, "stop_times", ["trip_id", "stop_sequence", "stop_id", "arrival_s", "departure_s"], st())

        days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        copy(cur, "calendar", ["service_id", *days, "start_date", "end_date"],
             ((r["service_id"], *[r[d] == "1" for d in days], gtfs_date(r["start_date"]), gtfs_date(r["end_date"]))
              for r in rows(z, "calendar.txt")))
        copy(cur, "calendar_dates", ["service_id", "date", "exception_type"],
             ((r["service_id"], gtfs_date(r["date"]), int(r["exception_type"])) for r in rows(z, "calendar_dates.txt")))
        copy(cur, "shapes", ["shape_id", "seq", "lat", "lon"],
             ((r["shape_id"], int(r["shape_pt_sequence"]), float(r["shape_pt_lat"]), float(r["shape_pt_lon"]))
              for r in rows(z, "shapes.txt")))

        cur.execute("""
            INSERT INTO trip_windows
            SELECT t.trip_id, t.route_id, t.service_id, t.direction_id, min(st.arrival_s), max(st.arrival_s)
            FROM trips t JOIN stop_times st USING (trip_id)
            GROUP BY t.trip_id, t.route_id, t.service_id, t.direction_id
        """)
        print(f"  trip_windows: {cur.rowcount:,} rows")
        cur.execute("ANALYZE")
    print("Static GTFS loaded.")


if __name__ == "__main__":
    main()
