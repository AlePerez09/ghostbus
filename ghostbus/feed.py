"""Runs inside the web server: prepares the database on first boot, then keeps bus data flowing.

FEED_MODE (env):
  auto  - realtime feed if GTFS_RT_API_KEY is set, otherwise the simulator (default)
  live  - realtime feed only
  sim   - simulator only
  off   - serve the app without collecting data (e.g. a second server instance)
"""
import os
import tempfile
import threading
import traceback
from datetime import datetime, timezone

import psycopg

from . import load_static, setup_db
from .config import DATABASE_URL, GTFS_RT_API_KEY, GTFS_STATIC_URL
from .tracker import Tracker

FEED_MODE = os.environ.get("FEED_MODE", "auto").lower()
BACKFILL_HOURS = float(os.environ.get("BACKFILL_HOURS", "3"))

status = {"phase": "starting", "mode": None, "last_tick": None, "buses": 0, "error": None, "log": []}
_stop = threading.Event()


def log(msg):
    print(msg, flush=True)
    status["log"] = (status["log"] + [msg])[-20:]


def _tick(buses, _arrivals):
    status["last_tick"] = datetime.now(timezone.utc).isoformat()
    status["buses"] = buses


def bootstrap():
    """Create tables and load the schedule if this database is brand new. Safe to run every boot."""
    with psycopg.connect(DATABASE_URL) as conn:
        has_schema = conn.execute("SELECT to_regclass('route_reliability_15m') IS NOT NULL").fetchone()[0]
    if not has_schema:
        status["phase"] = "creating database"
        log("First boot: creating tables, hypertables and continuous aggregates...")
        setup_db.main()
    with psycopg.connect(DATABASE_URL) as conn:
        routes = conn.execute("SELECT count(*) FROM routes").fetchone()[0]
    if routes == 0:
        status["phase"] = "loading schedule"
        log("Loading the Miami-Dade schedule...")
        try:
            load_static.load(GTFS_STATIC_URL)
        except Exception as ex:
            log(f"County schedule download failed ({ex}); using the built-in sample schedule.")
            from .fake_gtfs import main as make_fake
            path = os.path.join(tempfile.gettempdir(), "ghostbus_fake_gtfs.zip")
            make_fake(path)
            load_static.load(path)


def run():
    try:
        bootstrap()
        mode = FEED_MODE
        if mode == "auto":
            mode = "live" if GTFS_RT_API_KEY else "sim"
        status["mode"] = mode
        if mode == "off":
            status["phase"] = "ready (feed off)"
            return
        with psycopg.connect(DATABASE_URL) as conn:
            tracker = Tracker(conn)
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
    except Exception as ex:
        status["phase"] = "error"
        status["error"] = str(ex)
        log(traceback.format_exc())


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
    return t


def stop():
    _stop.set()
