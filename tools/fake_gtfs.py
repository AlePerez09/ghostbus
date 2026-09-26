"""Generate a small Miami-flavored GTFS zip for offline testing (no county download needed).

Usage: python tools/fake_gtfs.py fake_gtfs.zip
"""
import csv
import io
import math
import sys
import zipfile
from datetime import date, timedelta

# Rough corridors around FIU / West Miami-Dade: (route, name, [(lat, lon) waypoints], headway_min)
ROUTES = [
    ("8",   "SW 8 St / FIU",           [(25.7560, -80.3750), (25.7617, -80.3300), (25.7650, -80.2600), (25.7700, -80.1950)], 12),
    ("11",  "Flagler St",              [(25.7700, -80.3850), (25.7730, -80.3200), (25.7740, -80.2500), (25.7745, -80.1930)], 15),
    ("24",  "Coral Way",               [(25.7500, -80.3780), (25.7505, -80.3200), (25.7510, -80.2600), (25.7620, -80.2100)], 20),
    ("137", "W Dade Connection",       [(25.6860, -80.3830), (25.7200, -80.3830), (25.7560, -80.3790), (25.8120, -80.3800)], 30),
    ("71",  "SW 57 Ave",               [(25.6900, -80.2870), (25.7300, -80.2870), (25.7700, -80.2865), (25.8100, -80.2860)], 25),
    ("212", "Sweetwater Circulator",   [(25.7560, -80.3750), (25.7650, -80.3720), (25.7720, -80.3780), (25.7620, -80.3850)], 30),
]
STOP_SPACING_M = 600
SPEED_MPS = 6.5  # ~14.5 mph average including dwell


def dist(a, b):
    dy = (b[0] - a[0]) * 111_000
    dx = (b[1] - a[1]) * 111_000 * math.cos(math.radians(a[0]))
    return math.hypot(dx, dy)


def densify(pts):
    out = [pts[0]]
    for a, b in zip(pts, pts[1:]):
        n = max(1, int(dist(a, b) // STOP_SPACING_M))
        for i in range(1, n + 1):
            out.append((a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n))
    return out


def hms(s):
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def main(path):
    files = {k: io.StringIO() for k in ["agency", "routes", "stops", "trips", "stop_times", "calendar", "shapes"]}
    w = {k: csv.writer(v) for k, v in files.items()}
    w["agency"].writerows([["agency_id", "agency_name", "agency_url", "agency_timezone"],
                           ["MDT", "Fake Miami-Dade Transit", "https://example.com", "America/New_York"]])
    w["routes"].writerow(["route_id", "route_short_name", "route_long_name", "route_type", "route_color"])
    w["stops"].writerow(["stop_id", "stop_name", "stop_lat", "stop_lon"])
    w["trips"].writerow(["route_id", "service_id", "trip_id", "direction_id", "trip_headsign", "shape_id"])
    w["stop_times"].writerow(["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"])
    w["shapes"].writerow(["shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"])
    start, end = date.today() - timedelta(days=30), date.today() + timedelta(days=60)
    w["calendar"].writerows([["service_id", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
                              "sunday", "start_date", "end_date"],
                             ["ALL", 1, 1, 1, 1, 1, 1, 1, start.strftime("%Y%m%d"), end.strftime("%Y%m%d")]])
    for rid, name, way, headway in ROUTES:
        w["routes"].writerow([rid, rid, name, 3, "0B6E4F"])
        pts = densify(way)
        stop_ids = []
        for i, (lat, lon) in enumerate(pts):
            sid = f"{rid}-{i}"
            stop_ids.append(sid)
            w["stops"].writerow([sid, f"{name} stop {i + 1}", f"{lat:.6f}", f"{lon:.6f}"])
        for direction in (0, 1):
            seq_pts = pts if direction == 0 else pts[::-1]
            seq_ids = stop_ids if direction == 0 else stop_ids[::-1]
            shape_id = f"{rid}-{direction}"
            for i, (lat, lon) in enumerate(seq_pts):
                w["shapes"].writerow([shape_id, f"{lat:.6f}", f"{lon:.6f}", i])
            offsets, acc = [0], 0
            for a, b in zip(seq_pts, seq_pts[1:]):
                acc += dist(a, b) / SPEED_MPS + 20
                offsets.append(int(acc))
            for n, dep in enumerate(range(5 * 3600, 25 * 3600, headway * 60)):   # 5:00 to 1:00 next day
                tid = f"{rid}-{direction}-{n}"
                w["trips"].writerow([rid, "ALL", tid, direction, name, shape_id])
                for seq, (sid, off) in enumerate(zip(seq_ids, offsets), 1):
                    w["stop_times"].writerow([tid, hms(dep + off), hms(dep + off), sid, seq])
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for k, v in files.items():
            z.writestr(f"{k}.txt", v.getvalue())
    print(f"Wrote {path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "fake_gtfs.zip")
