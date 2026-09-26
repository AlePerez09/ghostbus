"""Ghost Bus API + static dashboard.

Usage: uvicorn ghostbus.api:app --reload --port 8000   → open http://localhost:8000
"""
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from contextlib import asynccontextmanager

import threading
from collections import defaultdict
from functools import wraps

from fastapi import FastAPI, HTTPException, Path as PathParam, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from . import feed, learn
from .predict import horizon_band, predict
from .config import DATABASE_URL, TZ


@asynccontextmanager
async def lifespan(_app):
    feed.start()          # first-boot DB setup + bus data, in a background thread
    yield
    feed.stop()


# No interactive API docs in production: less surface for people poking around.
app = FastAPI(title="Ghost Bus", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

# ── Security: headers + per-IP rate limit ─────────────────────────────────────
CSP = ("default-src 'self'; "
       "script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: https://tile.openstreetmap.org; connect-src 'self'; "
       "manifest-src 'self'; worker-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'none'")
RATE_LIMIT = 600          # requests per IP per minute. One phone uses ~16; whole classrooms share one campus Wi-Fi IP
_hits: dict[str, list] = defaultdict(lambda: [0.0, 0])
_hits_lock = threading.Lock()


def client_ip(request: Request) -> str:
    """The visitor's real IP, in a way they can't fake.
    Render sits behind Cloudflare, which *overwrites* CF-Connecting-IP with the address that actually
    connected, so a visitor can't forge it. X-Forwarded-For is only appended to, so its left side is
    visitor-controlled and must not be trusted for rate limiting."""
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[-1].strip()      # rightmost = added by the proxy closest to us
    return request.scope["client"][0] if request.scope.get("client") else "?"


@app.middleware("http")
async def security(request: Request, call_next):
    ip = client_ip(request)
    now = time.monotonic()
    with _hits_lock:
        window = _hits[ip]
        if now - window[0] > 60:
            window[0], window[1] = now, 0
        window[1] += 1
        too_many = window[1] > RATE_LIMIT
        if len(_hits) > 50_000:            # don't let the tracker itself grow forever
            _hits.clear()
    if too_many:
        return JSONResponse({"detail": "Too many requests, slow down."}, status_code=429, headers={"Retry-After": "60"})
    try:
        response = await call_next(request)
    except PoolTimeout:
        response = JSONResponse({"detail": "Busy right now, try again in a few seconds."}, status_code=503,
                                headers={"Retry-After": "5"})
    response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "geolocation=(self), camera=(), microphone=()"
    response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response


def cached(seconds):
    """Share one result per argument set for a few seconds, so a crowd (or an attacker)
    hitting the same endpoint costs the database one query instead of thousands."""
    def deco(fn):
        store, lock = {}, threading.Lock()

        @wraps(fn)
        def wrapper(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items())))
            now = time.monotonic()
            with lock:
                hit = store.get(key)
                if hit and now - hit[0] < seconds:
                    return hit[1]
            value = fn(*args, **kwargs)
            with lock:
                if len(store) > 2000:
                    store.clear()
                store[key] = (now, value)
            return value
        return wrapper
    return deco


def ID():
    return PathParam(..., min_length=1, max_length=64)   # fresh object per parameter (FastAPI mutates it)
# At most 8 queries run at once no matter how many requests arrive; extras wait up to 3 s, then get
# a quick "busy" answer instead of queueing forever. This caps database load even if someone
# dodges the per-IP limit (e.g. with many real IPs).
pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=8, timeout=3, kwargs={"row_factory": dict_row, "options": "-c statement_timeout=15000"},
                      check=ConnectionPool.check_connection,   # test each connection before use; drop dead ones
                      max_idle=300, open=True)
WEB = Path(__file__).resolve().parent.parent / "web"
app.mount("/static", StaticFiles(directory=WEB), name="static")


def q(sql, params=None):
    for attempt in (1, 2):   # one retry if the database dropped the connection mid-query
        try:
            with pool.connection() as conn:
                return conn.execute(sql, params or {}).fetchall()
        except psycopg.OperationalError:
            if attempt == 2:
                raise


def service_clock(now: datetime):
    """[(service_date, seconds_since_that_day's_midnight)] for today and yesterday (for after-midnight trips)."""
    local = now.astimezone(TZ)
    out = []
    for d in (local.date(), local.date() - timedelta(days=1)):
        midnight = datetime(d.year, d.month, d.day, 12, tzinfo=TZ) - timedelta(hours=12)
        out.append((d, int((now - midnight).total_seconds()), midnight))
    return out


@app.api_route("/", methods=["GET", "HEAD"])
def index():
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})


# PWA files must be served from the site root so the service worker controls the whole app.
@app.get("/sw.js")
def service_worker():
    return FileResponse(WEB / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(WEB / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/api/health")
def health():
    st = feed.status
    mode = st["mode"]
    if not str(st["phase"]).startswith("live"):      # viewer/standby: report what the writing server runs
        mode = writer_mode() or mode
    # Public: only coarse status. Full errors and logs stay in the server logs.
    return {"phase": st["phase"], "mode": mode, "last_tick": st["last_tick"], "buses": st["buses"],
            "ok": st["error"] is None,
            "feed_fresh": feed_fresh() if st["phase"] not in ("starting", "creating database", "loading schedule") else None}


@cached(60)
def writer_mode():
    try:
        row = q("SELECT value FROM meta WHERE key = 'feed_mode'")
        return row[0]["value"] if row else None
    except Exception:
        return None


@cached(10)
def feed_fresh() -> bool:
    """Is bus data actually arriving? If the feed is down, every bus would look like a 'ghost',
    so ghost alerts and live ETAs are paused instead of crying wolf."""
    row = q("SELECT max(time) > now() - interval '3 minutes' AS fresh FROM vehicle_positions "
            "WHERE time > now() - interval '1 hour'")
    return bool(row and row[0]["fresh"])


# ── Live map ─────────────────────────────────────────────────────────────────
@app.get("/api/live")
@cached(5)
def live():
    return q("""
        SELECT DISTINCT ON (vp.vehicle_id)
               vp.vehicle_id, vp.route_id, r.route_short_name, vp.trip_id, vp.lat, vp.lon, vp.bearing,
               vp.time, d.delay_s
        FROM vehicle_positions vp
        LEFT JOIN routes r USING (route_id)
        LEFT JOIN LATERAL (
            SELECT delay_s FROM stop_arrivals sa
            WHERE sa.vehicle_id = vp.vehicle_id AND sa.time > now() - interval '15 minutes'
            ORDER BY sa.time DESC LIMIT 1) d ON TRUE
        WHERE vp.time > now() - interval '3 minutes'
        ORDER BY vp.vehicle_id, vp.time DESC
    """)


@app.get("/api/routes")
@cached(300)
def routes():
    return q("SELECT route_id, route_short_name, route_long_name, route_color FROM routes ORDER BY route_short_name")


@app.get("/api/routes/{route_id}/shape")
@cached(3600)
def route_shape(route_id: str = ID()):
    rows = q("""
        WITH s AS (SELECT shape_id, count(*) n FROM trips WHERE route_id = %(r)s AND shape_id IS NOT NULL
                   GROUP BY shape_id ORDER BY n DESC LIMIT 2)
        SELECT shape_id, array_agg(ARRAY[lat, lon] ORDER BY seq) AS coords
        FROM shapes WHERE shape_id IN (SELECT shape_id FROM s) GROUP BY shape_id
    """, {"r": route_id})
    return [r["coords"] for r in rows]


# ── Ghost buses: scheduled to be running right now, but nothing in the feed ──────────
@app.get("/api/ghosts")
@cached(10)
def ghosts():
    if not feed_fresh():
        return {"scheduled_now": None, "ghost_count": 0, "ghosts": [], "feed_down": True,
                "warning": "Live bus data is delayed right now, so ghost alerts are paused. Showing scheduled times."}
    now = datetime.now(timezone.utc)
    (d0, s0, _), (d1, s1, _) = service_clock(now)
    rows = q("""
        WITH scheduled AS (
            SELECT w.*, %(d0)s::date AS service_date, %(s0)s AS now_s FROM trip_windows w
            WHERE w.service_id IN (SELECT service_id FROM active_services(%(d0)s))
              AND %(s0)s BETWEEN w.start_s + 300 AND w.end_s
            UNION ALL
            SELECT w.*, %(d1)s::date, %(s1)s FROM trip_windows w
            WHERE w.service_id IN (SELECT service_id FROM active_services(%(d1)s))
              AND %(s1)s BETWEEN w.start_s + 300 AND w.end_s
        ),
        seen AS (SELECT DISTINCT trip_id FROM vehicle_positions WHERE time > now() - interval '10 minutes')
        SELECT s.trip_id, s.route_id, r.route_short_name, t.headsign, s.direction_id,
               (s.now_s - s.start_s) / 60 AS minutes_since_start, (s.end_s - s.now_s) / 60 AS minutes_left,
               (s.trip_id IN (SELECT trip_id FROM seen)) AS seen
        FROM scheduled s JOIN trips t USING (trip_id) LEFT JOIN routes r ON r.route_id = s.route_id
    """, {"d0": d0, "s0": s0, "d1": d1, "s1": s1})
    scheduled = len(rows)
    missing = [r for r in rows if not r["seen"]]
    for r in missing:
        r.pop("seen")
    missing.sort(key=lambda r: (r["route_short_name"] or "", r["minutes_since_start"]))
    # If almost nothing matches, the feed's trip_ids probably don't line up with the static GTFS.
    warning = None
    if scheduled and len(missing) / scheduled > 0.6:
        warning = "Most scheduled trips are missing — check that realtime trip_ids match the static GTFS."
    return {"scheduled_now": scheduled, "ghost_count": len(missing), "ghosts": missing, "warning": warning,
            "feed_down": False}


# ── Reliability leaderboard (reads the continuous aggregate) ─────────────────────
@app.get("/api/reliability")
@cached(30)
def reliability(hours: float = Query(3, gt=0, le=168)):
    rows = q("""
        SELECT c.route_id, r.route_short_name, r.route_long_name,
               sum(arrivals)::int AS arrivals, sum(headways)::int AS headways,
               sum(bunched)::int AS bunched, sum(long_gaps)::int AS long_gaps,
               sum(avg_headway_s * headways) / nullif(sum(headways), 0) AS avg_headway_s,
               sum(sd_headway_s * headways) / nullif(sum(headways), 0) AS sd_headway_s,
               sum(avg_delay_s * arrivals) / nullif(sum(arrivals), 0) AS avg_delay_s
        FROM route_reliability_15m c LEFT JOIN routes r USING (route_id)
        WHERE bucket > now() - make_interval(secs => %(h)s * 3600)
        GROUP BY 1, 2, 3 HAVING sum(headways) >= 5
    """, {"h": hours})
    for r in rows:
        cv = (r["sd_headway_s"] or 0) / r["avg_headway_s"] if r["avg_headway_s"] else 0
        bunched_share = r["bunched"] / r["headways"] if r["headways"] else 0
        r["headway_cv"] = round(cv, 2)
        r["bunched_pct"] = round(100 * bunched_share, 1)
        # 100 = evenly spaced buses, none bunched. Loses points for bunching and irregular spacing.
        r["score"] = round(100 * (1 - bunched_share) * max(0.0, 1 - cv / 2))
    rows.sort(key=lambda r: r["score"])
    return rows


@app.get("/api/routes/{route_id}/timeline")
@cached(30)
def route_timeline(route_id: str = ID(), hours: float = Query(6, gt=0, le=168)):
    return q("""
        SELECT bucket, sum(arrivals)::int AS arrivals, sum(bunched)::int AS bunched,
               sum(long_gaps)::int AS long_gaps, avg(avg_headway_s) AS avg_headway_s,
               avg(avg_delay_s) AS avg_delay_s, max(p80_delay_s) AS p80_delay_s
        FROM route_reliability_15m
        WHERE route_id = %(r)s AND bucket > now() - make_interval(secs => %(h)s * 3600)
        GROUP BY bucket ORDER BY bucket
    """, {"r": route_id, "h": hours})


# ── Leave-now planner ───────────────────────────────────────────────────────────
@app.get("/api/stops/search")
@cached(60)
def stop_search(q_: str = Query(..., alias="q", min_length=2, max_length=60)):
    return q("SELECT stop_id, stop_name, lat, lon FROM stops WHERE stop_name ILIKE %(p)s OR stop_id = %(s)s "
             "ORDER BY stop_name LIMIT 15", {"p": f"%{q_}%", "s": q_})


# ── Address search (geocoding) ────────────────────────────────────────────────────
# Uses OpenStreetMap's Nominatim. Its usage policy: identify the app, at most 1 request/second,
# cache results, and no search-as-you-type. So the app only geocodes when the rider presses Search,
# every server shares one throttle, and answers are cached for a day. Addresses are not stored.
MIAMI_VIEWBOX = "-80.95,26.05,-80.05,25.10"      # west,north,east,south: Miami-Dade plus a margin
_geo_cache: dict = {}
_geo_lock = threading.Lock()
_geo_last = [0.0]


@app.get("/api/geocode")
def geocode(q_: str = Query(..., alias="q", min_length=3, max_length=120)):
    import requests
    key = " ".join(q_.lower().split())
    hit = _geo_cache.get(key)
    if hit and time.time() - hit[0] < 86400:
        return hit[1]
    with _geo_lock:                                  # one request per second, across all riders
        wait = 1.05 - (time.time() - _geo_last[0])
        if wait > 0:
            time.sleep(wait)
        _geo_last[0] = time.time()
        try:
            resp = requests.get("https://nominatim.openstreetmap.org/search", timeout=8, params={
                "q": q_, "format": "jsonv2", "limit": 5, "countrycodes": "us",
                "viewbox": MIAMI_VIEWBOX, "bounded": 1, "addressdetails": 0,
            }, headers={"User-Agent": "GhostBus/1.0 (+https://github.com/AlePerez09/ghostbus)"})
            resp.raise_for_status()
            raw = resp.json()
        except Exception:
            raise HTTPException(503, "Address search is busy right now. Try again in a moment.")
    out = []
    for r in raw:
        parts = [p.strip() for p in r.get("display_name", "").split(",")]
        for i in range(len(parts) - 1):          # "11200, Southwest 8th Street" -> "11200 Southwest 8th Street"
            if parts[i].isdigit():
                parts[i:i + 2] = [f"{parts[i]} {parts[i + 1]}"]
                break
        out.append({"label": ", ".join(parts[:3]), "detail": ", ".join(parts[3:5]),
                    "lat": float(r["lat"]), "lon": float(r["lon"])})
    if len(_geo_cache) > 5000:
        _geo_cache.clear()
    _geo_cache[key] = (time.time(), out)
    return out


@app.get("/api/stops/near")
def stops_near(lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180),
               limit: int = Query(8, ge=1, le=50)):
    return q("""
        SELECT stop_id, stop_name, lat, lon,
               round((111000 * sqrt(power(lat - %(lat)s, 2) + power((lon - %(lon)s) * cos(radians(%(lat)s)), 2)))::numeric) AS meters
        FROM stops ORDER BY power(lat - %(lat)s, 2) + power((lon - %(lon)s) * cos(radians(%(lat)s)), 2) LIMIT %(n)s
    """, {"lat": lat, "lon": lon, "n": limit})


@app.get("/api/stops/{stop_id}/leave")
@cached(10)
def leave_now(stop_id: str = ID(), walk_min: int = Query(5, ge=0, le=60)):
    """Next buses at a stop with a time window, a one-word confidence and a plain-language 'why'."""
    stop = q("SELECT stop_id, stop_name, lat, lon FROM stops WHERE stop_id = %(s)s", {"s": stop_id})
    if not stop:
        raise HTTPException(404, "Unknown stop")
    now = datetime.now(timezone.utc)
    live_ok = feed_fresh()
    cands = []
    for d, now_s, midnight in service_clock(now):
        rows = q("""
            SELECT st.trip_id, st.stop_sequence, st.arrival_s, t.route_id, t.direction_id, r.route_short_name,
                   t.headsign, w.start_s,
                   live.vehicle_id, live.stop_sequence AS bus_seq, cur.arrival_s AS bus_sched_s,
                   ld.delay_s AS live_delay_s,
                   (SELECT 1 FROM stop_arrivals sa WHERE sa.trip_id = st.trip_id AND sa.stop_id = st.stop_id
                      AND sa.time > now() - interval '3 hours' LIMIT 1) AS already_passed
            FROM stop_times st
            JOIN trips t USING (trip_id)
            JOIN trip_windows w USING (trip_id)
            LEFT JOIN routes r ON r.route_id = t.route_id
            LEFT JOIN LATERAL (SELECT vehicle_id, stop_sequence, time FROM vehicle_positions vp
                               WHERE vp.trip_id = st.trip_id AND vp.time > now() - interval '3 minutes'
                               ORDER BY time DESC LIMIT 1) live ON TRUE
            LEFT JOIN stop_times cur ON cur.trip_id = st.trip_id AND cur.stop_sequence = live.stop_sequence
            LEFT JOIN LATERAL (SELECT delay_s FROM stop_arrivals sa WHERE sa.trip_id = st.trip_id
                               AND sa.time > now() - interval '3 hours' ORDER BY time DESC LIMIT 1) ld ON TRUE
            WHERE st.stop_id = %(stop)s
              AND t.service_id IN (SELECT service_id FROM active_services(%(d)s))
              AND st.arrival_s BETWEEN %(now_s)s - 1800 AND %(now_s)s + 5400
        """, {"stop": stop_id, "d": d, "now_s": now_s})
        for r in rows:
            if not r["already_passed"]:
                cands.append((r, midnight + timedelta(seconds=r["arrival_s"]), now_s))

    profs = learn.profiles(q, [(stop_id, r["route_id"], learn.local_hour(sched)) for r, sched, _ in cands])
    ghosts = learn.ghost_risks(q, [(r["route_id"], r["direction_id"], r["start_s"]) for r, _, _ in cands])
    scales = calibration()
    out = []
    for r, sched, now_s in cands:
        is_live = bool(r["vehicle_id"]) and live_ok
        days = ghosts.get((r["route_id"], r["direction_id"], r["start_s"]))
        stops_away = (r["stop_sequence"] - r["bus_seq"]) if is_live and r["bus_seq"] is not None else None
        mins_away = ((r["arrival_s"] - r["bus_sched_s"]) / 60) if is_live and r["bus_sched_s"] is not None else None
        missing = live_ok and not r["vehicle_id"] and now_s > r["start_s"] + 300
        p = predict(live=is_live, current_delay_s=r["live_delay_s"] if is_live else None,
                    minutes_away=mins_away, stops_away=stops_away,
                    profile=profs.get((stop_id, r["route_id"], learn.local_hour(sched))),
                    ghost_risk=learn.risk_from(days), ghost_days=days, started=now_s > r["start_s"],
                    scale=scales.get(horizon_band(mins_away), 1.0) if mins_away is not None else 1.0)
        why, confidence = list(p.why), p.confidence
        if missing:
            late_by = round((now_s - r["start_s"]) / 60)
            confidence = "may not come"
            why = [f"This bus should have started its trip {late_by} min ago but hasn't sent a signal. "
                   f"It may be a ghost bus."] + [w for w in why if w.startswith("Heads up")]
        lo, eta, hi = (sched + timedelta(seconds=x) for x in (p.lo_s, p.delay_s, p.hi_s))
        if hi < now - timedelta(minutes=1):
            continue
        out.append({
            "route_id": r["route_id"], "route_short_name": r["route_short_name"], "headsign": r["headsign"],
            "trip_id": r["trip_id"], "scheduled": sched, "eta": None if missing else eta,
            "window": None if missing else [max(lo, now), hi],
            "confidence": confidence, "why": why, "live": is_live, "stops_away": stops_away,
            "ghost_risk": learn.risk_from(days),
            "leave_at": None if missing else max(lo, now) - timedelta(minutes=walk_min),
            # kept for older app versions
            "basis": "ghost" if missing else ("live" if is_live else ("history" if profs else "schedule")),
            "delay_min": None if missing else round(p.delay_s / 60, 1),
        })
    out.sort(key=lambda x: x["eta"] or x["scheduled"])
    catchable = [x for x in out if x["leave_at"] and x["leave_at"] >= now and x["confidence"] != "may not come"]
    rec = catchable[0] if catchable else None
    backup = catchable[1] if len(catchable) > 1 else None
    return {"stop": stop[0], "now": now, "walk_min": walk_min, "live_ok": live_ok,
            "recommendation": rec, "backup": backup, "arrivals": out[:12],
            "leave_rule": "We time your walk for the early end of the window, so the bus won't beat you there."}


@cached(60)
def calibration():
    try:
        return learn.get_scales(q)
    except Exception:
        return {}


@app.get("/api/accuracy")
@cached(120)
def accuracy(days: int = Query(7, ge=1, le=30), route_id: str | None = Query(None, max_length=64)):
    """How often our live predictions were right, by how far ahead they were made."""
    rows = q("""
        SELECT horizon_band, sum(n)::int AS n, sum(within_2min)::int AS within_2min,
               sum(in_window)::int AS in_window, sum(avg_abs_error_s * n) / nullif(sum(n), 0) AS avg_abs_error_s
        FROM accuracy_hourly
        WHERE bucket > now() - make_interval(days => %(d)s) AND live
          AND (%(r)s::text IS NULL OR route_id = %(r)s)
        GROUP BY horizon_band ORDER BY horizon_band
    """, {"d": days, "r": route_id})
    label = {5: "up to 5 min ahead", 10: "5–10 min ahead", 20: "10–20 min ahead", 30: "20+ min ahead"}
    bands = [{"band": r["horizon_band"], "label": label.get(r["horizon_band"], ""), "n": r["n"],
              "within_2min_pct": round(100 * r["within_2min"] / r["n"]) if r["n"] else None,
              "in_window_pct": round(100 * r["in_window"] / r["n"]) if r["n"] else None,
              "avg_error_min": round(r["avg_abs_error_s"] / 60, 1) if r["avg_abs_error_s"] is not None else None}
             for r in rows]
    total = sum(b["n"] for b in bands)
    in_win = sum((b["in_window_pct"] or 0) * b["n"] for b in bands) / total if total else None
    cal = q("SELECT updated FROM meta WHERE key = 'calibration'")
    recent = q("""SELECT count(*)::int AS n, count(*) FILTER (WHERE in_window)::int AS hit FROM prediction_results
                  WHERE time > now() - INTERVAL '2 hours' AND live""")[0]
    return {"days": days, "graded": total, "in_window_pct": round(in_win) if in_win is not None else None,
            "bands": bands, "goal_in_window_pct": 80,
            "last_2h_in_window_pct": round(100 * recent["hit"] / recent["n"]) if recent["n"] else None,
            "last_2h_graded": recent["n"],
            "calibration": calibration(), "calibrated_at": cal[0]["updated"] if cal else None}


# ── Under the hood: Tiger Data stats for the demo ────────────────────────────────
@app.get("/api/tiger")
@cached(60)
def tiger():
    stats = {}
    for ht in ("vehicle_positions", "stop_arrivals"):
        r = q(f"""
            SELECT approximate_row_count('{ht}') AS rows,
                   (SELECT count(*) FROM timescaledb_information.chunks WHERE hypertable_name = '{ht}') AS chunks,
                   (SELECT count(*) FROM timescaledb_information.chunks
                     WHERE hypertable_name = '{ht}' AND is_compressed) AS compressed_chunks,
                   hypertable_size('{ht}') AS bytes
        """)[0]
        c = q(f"SELECT sum(before_compression_total_bytes) b, sum(after_compression_total_bytes) a "
              f"FROM chunk_compression_stats('{ht}') WHERE compression_status = 'Compressed'")[0]
        r["compression_ratio"] = round(c["b"] / c["a"], 1) if c["a"] else None
        stats[ht] = r

    # Same question answered two ways: scan raw events vs read the continuous aggregate.
    raw_sql = """SELECT route_id, count(*), count(*) FILTER (WHERE headway_s < 180), avg(headway_s)
                 FROM stop_arrivals WHERE time > now() - interval '24 hours' GROUP BY route_id"""
    agg_sql = """SELECT route_id, sum(arrivals), sum(bunched), avg(avg_headway_s)
                 FROM route_reliability_15m WHERE bucket > now() - interval '24 hours' GROUP BY route_id"""
    timings = {}
    with pool.connection() as conn:
        for name, sql in (("raw_scan_ms", raw_sql), ("continuous_aggregate_ms", agg_sql)):
            conn.execute(sql).fetchall()              # warm-up
            t0 = time.perf_counter()
            conn.execute(sql).fetchall()
            timings[name] = round((time.perf_counter() - t0) * 1000, 2)
    return {"hypertables": stats, "benchmark_24h_leaderboard": timings}
