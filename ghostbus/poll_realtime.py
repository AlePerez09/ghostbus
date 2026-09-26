"""Poll a GTFS-realtime VehiclePositions feed forever and store it in Tiger Data.

Usage: python -m ghostbus.poll_realtime
Polls every agency in config.AGENCIES that has a realtime URL (Miami-Dade: set GTFS_RT_API_KEY;
others: BCT_RT_URL / PT_RT_URL / KW_RT_URL, plus *_RT_KEY if the feed needs one).
"""
import time
from datetime import datetime, timezone

import psycopg
import requests
from google.transit import gtfs_realtime_pb2

from .config import DATABASE_URL, POLL_SECONDS, RT_AGENCIES
from .tracker import Obs, Tracker

STATUS = {0: "INCOMING_AT", 1: "STOPPED_AT", 2: "IN_TRANSIT_TO"}


def fetch(log=print):
    """Poll every realtime agency. One agency failing doesn't drop the others; if all fail, re-raise."""
    out, errors = [], []
    for a in RT_AGENCIES:
        try:
            out += fetch_agency(a)
        except Exception as ex:
            errors.append(ex)
            log(f"{datetime.now():%H:%M:%S}  {a['name']} feed failed: {ex}")
    if errors and len(errors) == len(RT_AGENCIES):
        raise errors[0]
    return out


def fetch_agency(a):
    p = lambda v: f"{a['prefix']}{v}" if v else None     # same ID prefix the schedule loader used
    headers = {a["rt_header"]: a["rt_key"]} if a["rt_key"] else {}
    resp = requests.get(a["rt_url"], headers=headers, timeout=20)
    resp.raise_for_status()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(resp.content)
    now = datetime.now(timezone.utc)
    out = []
    for e in feed.entity:
        if not e.HasField("vehicle") or not e.vehicle.HasField("position"):
            continue
        v = e.vehicle
        ts = datetime.fromtimestamp(v.timestamp, timezone.utc) if v.timestamp else now
        trip = v.trip if v.HasField("trip") else None
        out.append(Obs(
            time=ts,
            vehicle_id=p(v.vehicle.id or v.vehicle.label or e.id),
            trip_id=p(trip.trip_id) if trip else None,
            route_id=p(trip.route_id) if trip else None,
            direction_id=trip.direction_id if trip and trip.HasField("direction_id") else None,
            lat=v.position.latitude, lon=v.position.longitude,
            bearing=v.position.bearing if v.position.HasField("bearing") else None,
            speed_mps=v.position.speed if v.position.HasField("speed") else None,
            stop_sequence=v.current_stop_sequence if v.HasField("current_stop_sequence") else None,
            status=STATUS.get(v.current_status) if v.HasField("current_status") else None,
            start_date=(trip.start_date or None) if trip else None,
        ))
    return out


def poll_loop(conn, tracker, stop=None, log=print, on_tick=None):
    """Poll the realtime feed until `stop` (a threading.Event) is set.
    Backs off when the feed errors or rate-limits us, so we never hammer the county's API."""
    failures = 0
    while not (stop and stop.is_set()):
        started = time.time()
        wait = POLL_SECONDS
        pos = arr = []
        try:
            obs = fetch(log)
            pos, arr = tracker.process(obs)
            tracker.write(pos, arr)
            failures = 0
            ticks = getattr(tracker, "_ticks", 0) + 1
            tracker._ticks = ticks
            if ticks % 40 == 0:
                tracker.prune({o.trip_id for o in obs if o.trip_id}, datetime.now(timezone.utc))
            log(f"{datetime.now():%H:%M:%S}  {len(pos):4d} buses  {len(arr):4d} new stop arrivals")
        except psycopg.OperationalError:
            raise                                   # database connection lost: supervisor reconnects
        except requests.HTTPError as ex:
            conn.rollback()
            failures += 1
            retry_after = ex.response.headers.get("Retry-After") if ex.response is not None else None
            wait = int(retry_after) if retry_after and retry_after.isdigit() else min(POLL_SECONDS * 2 ** failures, 300)
            log(f"{datetime.now():%H:%M:%S}  feed HTTP {ex.response.status_code if ex.response is not None else '?'}; retrying in {wait}s")
        except Exception as ex:  # network blips, malformed data
            conn.rollback()
            failures += 1
            wait = min(POLL_SECONDS * 2 ** failures, 300)
            log(f"{datetime.now():%H:%M:%S}  poll error: {ex}; retrying in {wait}s")
        if on_tick:
            on_tick(len(pos), len(arr))
        wait = max(1.0, wait - (time.time() - started))
        if stop:
            stop.wait(wait)
        else:
            time.sleep(wait)


def main():
    if not RT_AGENCIES:
        print("No realtime feeds configured (set GTFS_RT_API_KEY for Miami-Dade); nothing to poll.")
        return
    with psycopg.connect(DATABASE_URL) as conn:
        tracker = Tracker(conn)
        names = ", ".join(a["name"] for a in RT_AGENCIES)
        print(f"Polling {names} every {POLL_SECONDS}s. Ctrl+C to stop.")
        poll_loop(conn, tracker)


if __name__ == "__main__":
    main()
