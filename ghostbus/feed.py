"""Runs inside the web server: prepares the database, then keeps bus data flowing — and recovers by itself.

FEED_MODE (env):
  auto  - realtime feed if GTFS_RT_API_KEY is set, otherwise the simulator (default)
  live  - realtime feed only
  sim   - simulator only
  off   - serve the app without collecting data

Safety nets built in:
  * Supervisor: if the database or feed drops (network blip, Tiger maintenance, bad response),
    the feed reconnects with increasing back-off instead of dying silently.
  * Single writer: a Postgres advisory lock guarantees only ONE server writes bus data at a time,
    even if the app runs on Render and a laptop against the same database. Others wait on standby.
  * Migrations run on every boot (idempotent): retention policies, metadata table.
  * The bus schedule is refreshed weekly at 3 AM, so trip IDs keep matching the live feed.
"""
import os
import tempfile
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

import psycopg

from . import learn, load_static, setup_db
<<<<<<< HEAD
from .config import DATABASE_URL, GTFS_RT_API_KEY, GTFS_STATIC_URL, TZ
=======
from .config import AGENCIES, DATABASE_URL, RT_AGENCIES, TZ
>>>>>>> f967724 (Added Palm Beach, Broward, and Monroe Counties)
from .tracker import Tracker

FEED_MODE = os.environ.get("FEED_MODE", "auto").lower()
BACKFILL_HOURS = float(os.environ.get("BACKFILL_HOURS", "3"))
SCHEDULE_MAX_AGE_DAYS = 7
FEED_LOCK_ID = 7_274_274          # arbitrary app-wide number for pg advisory locks
BOOT_LOCK_ID = 7_274_275

status = {"phase": "starting", "mode": None, "last_tick": None, "buses": 0, "error": None, "log": [],
          "restarts": 0}
_stop = threading.Event()


class RestartFeed(Exception):
    """Raised to make the supervisor restart the feed cleanly (e.g. after a schedule reload)."""


def log(msg):
    print(msg, flush=True)
    status["log"] = (status["log"] + [msg])[-20:]


# ── database preparation ─────────────────────────────────────────────────────
MIGRATIONS = [
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT, updated TIMESTAMPTZ DEFAULT now())",
    # Keep raw GPS pings 14 days and stop events 90 days; the rollups (continuous aggregates) are kept forever.
    "SELECT add_retention_policy('vehicle_positions', INTERVAL '14 days', if_not_exists => TRUE)",
    "SELECT add_retention_policy('stop_arrivals', INTERVAL '90 days', if_not_exists => TRUE)",
<<<<<<< HEAD
]
=======
    # Multi-county: which agency each route/trip belongs to (older databases were Miami-Dade only).
    "ALTER TABLE routes ADD COLUMN IF NOT EXISTS agency TEXT DEFAULT 'mdt'",
    "ALTER TABLE trips ADD COLUMN IF NOT EXISTS agency TEXT DEFAULT 'mdt'",
    "ALTER TABLE trip_windows ADD COLUMN IF NOT EXISTS agency TEXT DEFAULT 'mdt'",
]
AGENCY_KEYS = ",".join(sorted(a["key"] for a in AGENCIES))
>>>>>>> f967724 (Added Palm Beach, Broward, and Monroe Counties)


def set_meta(conn, key, value):
    conn.execute("INSERT INTO meta (key, value, updated) VALUES (%s, %s, now()) "
                 "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated = now()", (key, value))


def load_schedule(allow_sample=True):
<<<<<<< HEAD
    """Load the county GTFS. Falls back to the built-in sample only when there's no schedule at all."""
    try:
        load_static.load(GTFS_STATIC_URL)
        source = "county"
=======
    """Load every county's GTFS. Falls back to the built-in sample only when there's no schedule at all.
    allow_sample=False (refreshes) keeps the current schedule unless every agency downloads."""
    try:
        loaded, failed = load_static.load_all(require_all=not allow_sample)
        if failed:
            log(f"Schedule download failed for {', '.join(failed)}; loaded {', '.join(loaded)}.")
        source = "county"
        agencies = ",".join(sorted(loaded))
>>>>>>> f967724 (Added Palm Beach, Broward, and Monroe Counties)
    except Exception as ex:
        if not allow_sample:
            log(f"Schedule refresh failed ({ex}); keeping the current schedule.")
            return False
        log(f"County schedule download failed ({ex}); using the built-in sample schedule.")
        from .fake_gtfs import main as make_fake
        path = os.path.join(tempfile.gettempdir(), "ghostbus_fake_gtfs.zip")
        make_fake(path)
        load_static.load(path)
        source = "sample"
<<<<<<< HEAD
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        set_meta(conn, "schedule_loaded_at", datetime.now(timezone.utc).isoformat())
        set_meta(conn, "schedule_source", source)
=======
        agencies = "mdt"
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        set_meta(conn, "schedule_loaded_at", datetime.now(timezone.utc).isoformat())
        set_meta(conn, "schedule_source", source)
        set_meta(conn, "schedule_agencies", agencies)
>>>>>>> f967724 (Added Palm Beach, Broward, and Monroe Counties)
    return True


def bootstrap():
    """Create tables and load the schedule if this database is brand new; run migrations every boot.
    A boot lock stops two servers from setting up the same empty database at the same time."""
    with psycopg.connect(DATABASE_URL, autocommit=True) as lock_conn:
        lock_conn.execute("SELECT pg_advisory_lock(%s)", (BOOT_LOCK_ID,))
        try:
            has_schema = lock_conn.execute("SELECT to_regclass('route_reliability_15m') IS NOT NULL").fetchone()[0]
            if not has_schema:
                status["phase"] = "creating database"
                log("First boot: creating tables, hypertables and continuous aggregates...")
                setup_db.main()
            for stmt in MIGRATIONS:
                lock_conn.execute(stmt)
            learn.migrate(lock_conn)          # history-learning tables + rollups
            routes = lock_conn.execute("SELECT count(*) FROM routes").fetchone()[0]
            if routes == 0:
                status["phase"] = "loading schedule"
<<<<<<< HEAD
                log("Loading the Miami-Dade schedule...")
                load_schedule(allow_sample=True)
=======
                log("Loading the bus schedules (Miami-Dade, Broward, Palm Beach, Monroe)...")
                load_schedule(allow_sample=True)
            elif (lock_conn.execute("SELECT value FROM meta WHERE key = 'schedule_agencies'").fetchone()
                  or ["mdt"])[0] != AGENCY_KEYS:
                status["phase"] = "loading schedule"
                log("Agency list changed; loading all county schedules...")
                load_schedule(allow_sample=False)
>>>>>>> f967724 (Added Palm Beach, Broward, and Monroe Counties)
            elif not lock_conn.execute("SELECT 1 FROM meta WHERE key = 'schedule_loaded_at'").fetchone():
                set_meta(lock_conn, "schedule_loaded_at", datetime.now(timezone.utc).isoformat())
        finally:
            lock_conn.execute("SELECT pg_advisory_unlock(%s)", (BOOT_LOCK_ID,))


def schedule_is_stale():
    with psycopg.connect(DATABASE_URL) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schedule_loaded_at'").fetchone()
        ended = conn.execute("SELECT coalesce(max(end_date) < current_date, false) FROM calendar").fetchone()[0]
    if ended:
        return True
    if not row:
        return True
    return datetime.now(timezone.utc) - datetime.fromisoformat(row[0]) > timedelta(days=SCHEDULE_MAX_AGE_DAYS)


# ── the feed ─────────────────────────────────────────────────────────────────
_last_schedule_check = [0.0]
_last_job = {"predictions": 0.0, "outcomes": 0.0}


def _learning_loop():
    """Separate thread: every 5 min log fresh predictions and grade old ones; every 10 min record which
    trips never showed. Runs apart from the bus feed, with a query time limit, so a slow query can
    never freeze live data."""
    first = True
    while not _stop.wait(30 if first else 60):
        if not str(status["phase"]).startswith("live"):
            continue                       # only the server that writes bus data does this
        now = time.time()
        try:
            with psycopg.connect(DATABASE_URL, options="-c statement_timeout=60000") as c:
                if now - _last_job["predictions"] > 300:
                    _last_job["predictions"] = now
                    made = learn.record_predictions(c)
                    graded = learn.resolve_predictions(c)
                    log(f"learning: logged {made} predictions, graded {graded}")
                if now - _last_job["outcomes"] > 600:
                    _last_job["outcomes"] = now
                    n = learn.record_trip_outcomes(c, lookback_s=1800)
                    log(f"learning: recorded {n} trip outcomes")
                    for band, (old, new, n) in learn.calibrate(c).items():
                        log(f"self-calibration: {band}-min band window scale {old} -> {new} ({n} graded)")
            first = False
        except Exception as ex:
            log(f"learning job failed (feed unaffected): {ex}")


def _tick(buses, _arrivals):
    status["last_tick"] = datetime.now(timezone.utc).isoformat()
    status["buses"] = buses
    status["error"] = None
    # Once every 10 minutes: is it 3 AM and is the schedule a week old? Then refresh it.
    if time.time() - _last_schedule_check[0] > 600:
        _last_schedule_check[0] = time.time()
        if datetime.now(TZ).hour == 3 and schedule_is_stale():
            status["phase"] = "refreshing schedule"
            log("Refreshing the bus schedule (weekly)...")
            if load_schedule(allow_sample=False):
                raise RestartFeed("schedule reloaded")


def run_feed(mode):
    """One feed session on one connection. Returns/raises when it should be restarted."""
    with psycopg.connect(DATABASE_URL, keepalives=1, keepalives_idle=30,
                         options="-c statement_timeout=60000") as conn:   # no query may stall the feed
        got = conn.execute("SELECT pg_try_advisory_lock(%s)", (FEED_LOCK_ID,)).fetchone()[0]
        conn.commit()
        if not got:
            status["phase"] = "standby (another server is collecting data)"
            log("Another server already writes bus data to this database; standing by.")
            _stop.wait(60)
            return
        set_meta(conn, "feed_mode", mode)       # lets viewer-only servers label data as live or demo
        tracker = Tracker(conn)
        conn.commit()
        if mode == "live":
            from .poll_realtime import poll_loop
            status["phase"] = "live"
            log("Polling the realtime feed.")
            poll_loop(conn, tracker, stop=_stop, log=log, on_tick=_tick)
        else:
            from .simulate import Sim, backfill, live_loop
            sim = Sim(conn)
            recent = conn.execute(
                "SELECT EXISTS (SELECT 1 FROM vehicle_positions WHERE time > now() - interval '30 minutes')"
            ).fetchone()[0]
            conn.commit()
            if not recent and BACKFILL_HOURS > 0:
                status["phase"] = "generating history"
                backfill(conn, tracker, sim, BACKFILL_HOURS, log=log)
            status["phase"] = "live (simulated)"
            live_loop(conn, tracker, sim, stop=_stop, log=log, on_tick=_tick)


def run():
    """Supervisor: keeps setup + feed alive forever, backing off 5 s → 5 min on repeated failures."""
    backoff = 5
    booted = False
    while not _stop.is_set():
        try:
            if not booted:
                bootstrap()
                booted = True
            mode = FEED_MODE
            if mode == "auto":
<<<<<<< HEAD
                mode = "live" if GTFS_RT_API_KEY else "sim"
=======
                mode = "live" if RT_AGENCIES else "sim"
>>>>>>> f967724 (Added Palm Beach, Broward, and Monroe Counties)
            status["mode"] = mode
            if mode == "off":
                status["phase"] = "ready (feed off)"
                return
            started = time.time()
            run_feed(mode)
            if time.time() - started > 300:
                backoff = 5                        # it ran fine for a while; reset the back-off
        except RestartFeed as ex:
            log(f"Restarting feed: {ex}")
            backoff = 5
            continue
        except Exception as ex:
            status["error"] = str(ex)
            status["restarts"] += 1
            status["phase"] = f"reconnecting in {backoff}s"
            log(traceback.format_exc(limit=3))
            _stop.wait(backoff)
            backoff = min(backoff * 2, 300)
            continue
        status["error"] = None


def keep_awake():
    """Free hosts (e.g. Render) pause apps after ~15 idle minutes, which would stop the data feed.
    Pinging our own public URL every 10 minutes keeps it running."""
    import requests
    url = os.environ.get("KEEPALIVE_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    if not url:
        return
    while not _stop.wait(600):
        try:
            requests.get(url.rstrip("/") + "/api/health", timeout=20)
        except Exception as ex:
            log(f"keep-alive ping failed: {ex}")


def start():
    t = threading.Thread(target=run, name="ghostbus-feed", daemon=True)
    t.start()
    threading.Thread(target=keep_awake, name="ghostbus-keepalive", daemon=True).start()
    threading.Thread(target=_learning_loop, name="ghostbus-learning", daemon=True).start()
    return t


def stop():
    _stop.set()
