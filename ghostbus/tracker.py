"""Turns raw vehicle pings into stop-arrival events with headway and delay.

Both the live poller and the simulator feed observations through Tracker.process(),
so the analytics behave the same whether data is real or simulated.
"""
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import TZ

ARRIVAL_RADIUS_M = 100      # fallback when the feed has no stop_sequence
MAX_HEADWAY_S = 3 * 3600    # ignore "headways" across service breaks


@dataclass
class Obs:
    time: datetime
    vehicle_id: str
    trip_id: str | None
    route_id: str | None
    direction_id: int | None
    lat: float
    lon: float
    bearing: float | None = None
    speed_mps: float | None = None
    stop_sequence: int | None = None
    status: str | None = None           # IN_TRANSIT_TO | INCOMING_AT | STOPPED_AT
    start_date: str | None = None       # YYYYMMDD from the feed, if provided


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


class Tracker:
    def __init__(self, conn):
        self.conn = conn
        self.trips: dict[str, dict] = {}                   # trip_id -> {route_id, direction_id, stops:[...]}
        self.vehicle_state: dict[str, tuple] = {}          # vehicle_id -> (trip_id, passed_seq, time)
        self.last_arrival: dict[tuple, datetime] = {}      # (route, dir, stop) -> time
        self._seed_last_arrivals()

    # ── caches ──────────────────────────────────────────────────────────────
    def _seed_last_arrivals(self):
        rows = self.conn.execute("""
            SELECT DISTINCT ON (route_id, direction_id, stop_id) route_id, direction_id, stop_id, time
            FROM stop_arrivals WHERE time > now() - interval '3 hours'
            ORDER BY route_id, direction_id, stop_id, time DESC
        """).fetchall()
        for r, d, s, t in rows:
            self.last_arrival[(r, d, s)] = t

    def preload_trips(self, trip_ids):
        missing = [t for t in set(trip_ids) if t and t not in self.trips]
        if not missing:
            return
        rows = self.conn.execute("""
            SELECT t.trip_id, t.route_id, t.direction_id, st.stop_sequence, st.stop_id, st.arrival_s, s.lat, s.lon
            FROM trips t
            JOIN stop_times st USING (trip_id)
            JOIN stops s USING (stop_id)
            WHERE t.trip_id = ANY(%s)
            ORDER BY t.trip_id, st.stop_sequence
        """, (missing,)).fetchall()
        for trip_id, route_id, direction_id, seq, stop_id, arr, lat, lon in rows:
            info = self.trips.setdefault(trip_id, {"route_id": route_id, "direction_id": direction_id, "stops": []})
            info["stops"].append((seq, stop_id, arr, lat, lon))
        for t in missing:                      # remember unknown trips so we don't re-query every poll
            self.trips.setdefault(t, None)

    def forget_trips(self, trip_ids):
        for t in trip_ids:
            self.trips.pop(t, None)

    def prune(self, keep_trip_ids, now: datetime):
        """Drop cached trips and vehicles we haven't needed lately, so memory stays flat all weekend."""
        keep = set(keep_trip_ids)
        for t in [t for t in self.trips if t not in keep]:
            del self.trips[t]
        stale = now - timedelta(minutes=30)
        for v in [v for v, st in self.vehicle_state.items() if st[2] < stale]:
            del self.vehicle_state[v]
        old = now - timedelta(hours=3)
        for k in [k for k, t in self.last_arrival.items() if t < old]:
            del self.last_arrival[k]

    # ── core logic ──────────────────────────────────────────────────────────
    def _passed_seq(self, o: Obs, info, prev_passed):
        """Highest stop_sequence this bus has definitely reached."""
        if o.stop_sequence is not None:
            return o.stop_sequence if o.status == "STOPPED_AT" else o.stop_sequence - 1
        # No sequence in feed: snap to the nearest upcoming stop within ARRIVAL_RADIUS_M.
        best = None
        for seq, _sid, _arr, lat, lon in info["stops"]:
            if prev_passed is not None and seq <= prev_passed:
                continue
            d = haversine_m(o.lat, o.lon, lat, lon)
            if d <= ARRIVAL_RADIUS_M and (best is None or d < best[1]):
                best = (seq, d)
        return best[0] if best else prev_passed

    @staticmethod
    def _delay(t: datetime, sched_s: int, start_date: str | None):
        """Actual minus scheduled seconds. Picks the service day that makes the most sense."""
        local = t.astimezone(TZ)
        if start_date:
            days = [datetime.strptime(start_date, "%Y%m%d").date()]
        else:
            days = [local.date(), local.date() - timedelta(days=1)]
        best = None
        for d in days:
            # GTFS times count from "noon minus 12h", which equals local midnight except on DST days.
            midnight = datetime(d.year, d.month, d.day, 12, tzinfo=TZ) - timedelta(hours=12)
            delay = (t - (midnight + timedelta(seconds=sched_s))).total_seconds()
            if best is None or abs(delay) < abs(best):
                best = delay
        return int(best) if best is not None and abs(best) < 6 * 3600 else None

    def process(self, observations: list[Obs]):
        """Returns (position_rows, arrival_rows) ready to insert."""
        self.preload_trips(o.trip_id for o in observations)
        positions, arrivals = [], []
        for o in observations:
            info = self.trips.get(o.trip_id) if o.trip_id else None
            route_id = o.route_id or (info and info["route_id"])
            direction_id = o.direction_id if o.direction_id is not None else (info and info["direction_id"])
            positions.append((o.time, o.vehicle_id, route_id, o.trip_id, direction_id, o.lat, o.lon,
                              o.bearing, o.speed_mps, o.stop_sequence, o.status))
            if not info:
                continue

            prev = self.vehicle_state.get(o.vehicle_id)
            if prev and prev[2] >= o.time:
                continue                                    # stale / duplicate ping
            prev_passed = prev[1] if prev and prev[0] == o.trip_id else None
            passed = self._passed_seq(o, info, prev_passed)
            self.vehicle_state[o.vehicle_id] = (o.trip_id, passed if passed is not None else prev_passed, o.time)
            if prev_passed is None or passed is None or passed <= prev_passed:
                continue                                    # first sighting sets a baseline; no backfill

            new_stops = [s for s in info["stops"] if prev_passed < s[0] <= passed]
            t0 = prev[2]
            for i, (seq, stop_id, sched_s, _lat, _lon) in enumerate(new_stops, 1):
                # Spread skipped stops evenly between the previous ping and this one.
                t = t0 + (o.time - t0) * (i / len(new_stops))
                key = (route_id, direction_id, stop_id)
                last = self.last_arrival.get(key)
                headway = int((t - last).total_seconds()) if last else None
                if headway is not None and not (0 < headway <= MAX_HEADWAY_S):
                    headway = None
                if last is None or t > last:
                    self.last_arrival[key] = t
                arrivals.append((t, route_id, direction_id, stop_id, o.vehicle_id, o.trip_id,
                                 headway, self._delay(t, sched_s, o.start_date)))
        return positions, arrivals

    def write(self, positions, arrivals, bulk=False):
        with self.conn.cursor() as cur:
            if bulk:
                with cur.copy("COPY vehicle_positions (time, vehicle_id, route_id, trip_id, direction_id, lat, lon,"
                              " bearing, speed_mps, stop_sequence, status) FROM STDIN") as cp:
                    for r in positions:
                        cp.write_row(r)
            else:
                cur.executemany("""
                    INSERT INTO vehicle_positions (time, vehicle_id, route_id, trip_id, direction_id, lat, lon,
                                                   bearing, speed_mps, stop_sequence, status)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
                """, positions)
            if arrivals:
                with cur.copy("COPY stop_arrivals (time, route_id, direction_id, stop_id, vehicle_id, trip_id,"
                              " headway_s, delay_s) FROM STDIN") as cp:
                    for r in arrivals:
                        cp.write_row(r)
        self.conn.commit()
