"""Simulated realtime feed built from the static schedule — for testing and as a demo backup.

Buses follow their scheduled trips with realistic noise: random lateness, occasional
route-wide slowdowns (which cause bunching), and ~6% of trips that never show up (ghosts).

Usage:
    python -m ghostbus.simulate --backfill 4          # write the last 4 hours of history, then exit
    python -m ghostbus.simulate --backfill 4 --live   # ...then keep going in real time
"""
import argparse
import zlib
import random
import time
from datetime import datetime, timedelta, timezone

import psycopg

from .config import DATABASE_URL, POLL_SECONDS, TZ
from .tracker import Obs, Tracker

GHOST_RATE = 0.06


class Sim:
    def __init__(self, conn, seed=7):
        self.conn = conn
        self.rng = random.Random(seed)
        self.trip_delay: dict[str, float] = {}     # trip_id -> current delay (s); None = ghost
        self.incidents: dict[str, tuple] = {}      # route_id -> (until, extra_delay_s)
        self.windows_cache: dict = {}

    def service_day_start(self, d):
        return datetime(d.year, d.month, d.day, 12, tzinfo=TZ) - timedelta(hours=12)

    def windows(self, d):
        if d not in self.windows_cache:
            self.windows_cache[d] = self.conn.execute("""
                SELECT w.trip_id, w.route_id, w.start_s, w.end_s FROM trip_windows w
                WHERE w.service_id IN (SELECT service_id FROM active_services(%s))
            """, (d,)).fetchall()
        return self.windows_cache[d]

    def active_trips(self, t: datetime):
        local = t.astimezone(TZ)
        out = []
        for d in (local.date(), local.date() - timedelta(days=1)):
            sec = (t - self.service_day_start(d)).total_seconds()
            for trip_id, route_id, start_s, end_s in self.windows(d):
                if start_s - 60 <= sec <= end_s + 1800:     # generous: late buses run past end_s
                    out.append((trip_id, route_id, sec))
        return out

    def delay_for(self, trip_id, route_id, t, start_s=0):
        if trip_id not in self.trip_delay:
            # Real ghosts aren't random: the same runs go missing day after day (e.g. driver shortages).
            chronic = zlib.crc32(f"{route_id}-{start_s}".encode()) % 12 == 0
            ghost = self.rng.random() < (0.4 if chronic else GHOST_RATE / 2)
            self.trip_delay[trip_id] = None if ghost else self.rng.gauss(90, 120)
        d = self.trip_delay[trip_id]
        if d is None:
            return None
        # Random walk: buses gain/lose a little time each tick.
        d = max(-90.0, min(1500.0, d + self.rng.gauss(0.5, 10)))
        self.trip_delay[trip_id] = d
        inc = self.incidents.get(route_id)
        return d + (inc[1] if inc and inc[0] > t else 0)

    def maybe_incident(self, t, routes):
        # Each route gets a slowdown (crash, bridge opening, rain) roughly every 3 hours.
        for r in sorted(routes):
            if r not in self.incidents or self.incidents[r][0] < t:
                if self.rng.random() < POLL_SECONDS / (3 * 3600):
                    self.incidents[r] = (t + timedelta(minutes=self.rng.randint(20, 45)),
                                         self.rng.randint(240, 720))

    def tick(self, t: datetime, tracker: Tracker):
        active = self.active_trips(t)
        self.ticks = getattr(self, "ticks", 0) + 1
        if self.ticks % 40 == 0:                     # every ~10 simulated minutes
            ids = {tid for tid, _, _ in active}
            tracker.prune(ids, t)
            for tid in [tid for tid in self.trip_delay if tid not in ids]:
                del self.trip_delay[tid]
            for d in [d for d in self.windows_cache if d < (t.astimezone(TZ).date() - timedelta(days=1))]:
                del self.windows_cache[d]
        self.maybe_incident(t, {r for _, r, _ in active})
        tracker.preload_trips(tid for tid, _, _ in active)
        obs = []
        for trip_id, route_id, sec in active:
            info = tracker.trips.get(trip_id)
            if not info or len(info["stops"]) < 2:
                continue
            delay = self.delay_for(trip_id, route_id, t, info["stops"][0][2])
            if delay is None:
                continue                                   # ghost trip: never appears
            eff = sec - delay                              # where the schedule says we'd be `delay` ago
            stops = info["stops"]
            if eff < stops[0][2] or eff > stops[-1][2]:
                continue
            for a, b in zip(stops, stops[1:]):
                if a[2] <= eff <= b[2]:
                    span = max(1, b[2] - a[2])
                    f = (eff - a[2]) / span
                    lat, lon = a[3] + (b[3] - a[3]) * f, a[4] + (b[4] - a[4]) * f
                    at_stop = (eff - a[2]) < 20
                    obs.append(Obs(time=t, vehicle_id=f"sim-{trip_id}", trip_id=trip_id, route_id=route_id,
                                   direction_id=info["direction_id"], lat=lat, lon=lon,
                                   speed_mps=0.0 if at_stop else round(self.rng.uniform(4, 13), 1),
                                   stop_sequence=a[0] if at_stop else b[0],
                                   status="STOPPED_AT" if at_stop else "IN_TRANSIT_TO"))
                    break
        return obs


def refresh_aggregates():
    # CALL refresh_continuous_aggregate can't run inside a transaction, so use autocommit.
    with psycopg.connect(DATABASE_URL, autocommit=True) as ac:
        for view in ("route_reliability_15m", "stop_delay_hourly"):
            ac.execute(f"CALL refresh_continuous_aggregate('{view}', NULL, NULL)")


def backfill(conn, tracker, sim, hours, step=POLL_SECONDS, log=print):
    """Generate `hours` of history ending now, as fast as the database accepts it."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    t = now - timedelta(hours=hours)
    total_p = total_a = 0
    while t < now:
        pos, arr = tracker.process(sim.tick(t, tracker))
        tracker.write(pos, arr, bulk=True)
        total_p, total_a = total_p + len(pos), total_a + len(arr)
        if t.minute % 30 == 0 and t.second < step:
            log(f"  backfill {t.astimezone(TZ):%a %H:%M}  positions={total_p:,}  arrivals={total_a:,}")
        t += timedelta(seconds=step)
    log(f"Backfill done: {total_p:,} positions, {total_a:,} stop arrivals.")
    refresh_aggregates()


def live_loop(conn, tracker, sim, step=POLL_SECONDS, stop=None, log=print, on_tick=None):
    """Simulate in real time until `stop` (a threading.Event) is set."""
    while not (stop and stop.is_set()):
        started = time.time()
        t = datetime.now(timezone.utc).replace(microsecond=0)
        try:
            pos, arr = tracker.process(sim.tick(t, tracker))
            tracker.write(pos, arr)
            log(f"{t.astimezone(TZ):%H:%M:%S}  {len(pos):4d} buses  {len(arr):4d} new stop arrivals")
        except psycopg.OperationalError:
            raise                       # connection lost: let the supervisor reconnect
        except Exception as ex:
            conn.rollback()
            log(f"simulator error: {ex}")
            pos = arr = []
        if on_tick:
            on_tick(len(pos), len(arr))  # may raise RestartFeed on purpose
        wait = max(1.0, step - (time.time() - started))
        if stop:
            stop.wait(wait)
        else:
            time.sleep(wait)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=float, default=0, help="hours of history to generate first")
    ap.add_argument("--step", type=int, default=POLL_SECONDS, help="seconds between simulated pings")
    ap.add_argument("--live", action="store_true", help="keep simulating in real time afterwards")
    args = ap.parse_args()

    with psycopg.connect(DATABASE_URL) as conn:
        tracker, sim = Tracker(conn), Sim(conn)
        if args.backfill:
            backfill(conn, tracker, sim, args.backfill, args.step)
        if args.live:
            print(f"Simulating live every {args.step}s. Ctrl+C to stop.")
            live_loop(conn, tracker, sim, args.step)


if __name__ == "__main__":
    main()
