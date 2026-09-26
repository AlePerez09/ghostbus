"""Poll a GTFS-realtime VehiclePositions feed forever and store it in Tiger Data.

Usage: python -m ghostbus.poll_realtime
Needs GTFS_RT_API_KEY in .env (Miami-Dade realtime is served by Swiftly).
"""
import time
from datetime import datetime, timezone

import psycopg
import requests
from google.transit import gtfs_realtime_pb2

from .config import DATABASE_URL, GTFS_RT_API_KEY, GTFS_RT_AUTH_HEADER, GTFS_RT_VEHICLES_URL, POLL_SECONDS
from .tracker import Obs, Tracker

STATUS = {0: "INCOMING_AT", 1: "STOPPED_AT", 2: "IN_TRANSIT_TO"}


def fetch():
    headers = {GTFS_RT_AUTH_HEADER: GTFS_RT_API_KEY} if GTFS_RT_API_KEY else {}
    resp = requests.get(GTFS_RT_VEHICLES_URL, headers=headers, timeout=20)
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
            vehicle_id=(v.vehicle.id or v.vehicle.label or e.id),
            trip_id=(trip.trip_id or None) if trip else None,
            route_id=(trip.route_id or None) if trip else None,
            direction_id=trip.direction_id if trip and trip.HasField("direction_id") else None,
            lat=v.position.latitude, lon=v.position.longitude,
            bearing=v.position.bearing if v.position.HasField("bearing") else None,
            speed_mps=v.position.speed if v.position.HasField("speed") else None,
            stop_sequence=v.current_stop_sequence if v.HasField("current_stop_sequence") else None,
            status=STATUS.get(v.current_status) if v.HasField("current_status") else None,
            start_date=(trip.start_date or None) if trip else None,
        ))
    return out


def main():
    if not GTFS_RT_API_KEY:
        print("Warning: GTFS_RT_API_KEY is empty; the request will probably be rejected.")
    with psycopg.connect(DATABASE_URL) as conn:
        tracker = Tracker(conn)
        print(f"Polling {GTFS_RT_VEHICLES_URL} every {POLL_SECONDS}s. Ctrl+C to stop.")
        while True:
            started = time.time()
            try:
                obs = fetch()
                pos, arr = tracker.process(obs)
                tracker.write(pos, arr)
                print(f"{datetime.now():%H:%M:%S}  {len(pos):4d} buses  {len(arr):4d} new stop arrivals")
            except Exception as ex:  # keep polling through network blips
                conn.rollback()
                print(f"{datetime.now():%H:%M:%S}  error: {ex}")
            time.sleep(max(1, POLL_SECONDS - (time.time() - started)))


if __name__ == "__main__":
    main()
