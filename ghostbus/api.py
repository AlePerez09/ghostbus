"""Ghost Bus API + static dashboard.

Usage: uvicorn ghostbus.api:app --reload --port 8000   → open http://localhost:8000
"""
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import DATABASE_URL, TZ

app = FastAPI(title="Ghost Bus")
pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=8, kwargs={"row_factory": dict_row}, open=True)
WEB = Path(__file__).resolve().parent.parent / "web"
app.mount("/static", StaticFiles(directory=WEB), name="static")


def q(sql, params=None):
    with pool.connection() as conn:
        return conn.execute(sql, params or {}).fetchall()


def service_clock(now: datetime):
    """[(service_date, seconds_since_that_day's_midnight)] for today and yesterday (for after-midnight trips)."""
    local = now.astimezone(TZ)
    out = []
    for d in (local.date(), local.date() - timedelta(days=1)):
        midnight = datetime(d.year, d.month, d.day, 12, tzinfo=TZ) - timedelta(hours=12)
        out.append((d, int((now - midnight).total_seconds()), midnight))
    return out


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


# ── Live map ─────────────────────────────────────────────────────────────────
@app.get("/api/live")
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
def routes():
    return q("SELECT route_id, route_short_name, route_long_name, route_color FROM routes ORDER BY route_short_name")


@app.get("/api/routes/{route_id}/shape")
def route_shape(route_id: str):
    rows = q("""
        WITH s AS (SELECT shape_id, count(*) n FROM trips WHERE route_id = %(r)s AND shape_id IS NOT NULL
                   GROUP BY shape_id ORDER BY n DESC LIMIT 2)
        SELECT shape_id, array_agg(ARRAY[lat, lon] ORDER BY seq) AS coords
        FROM shapes WHERE shape_id IN (SELECT shape_id FROM s) GROUP BY shape_id
    """, {"r": route_id})
    return [r["coords"] for r in rows]


# ── Ghost buses: scheduled to be running right now, but nothing in the feed ──────────
@app.get("/api/ghosts")
def ghosts():
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
    return {"scheduled_now": scheduled, "ghost_count": len(missing), "ghosts": missing, "warning": warning}


# ── Reliability leaderboard (reads the continuous aggregate) ─────────────────────
@app.get("/api/reliability")
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
def route_timeline(route_id: str, hours: float = Query(6, gt=0, le=168)):
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
def stop_search(q_: str = Query(..., alias="q", min_length=2)):
    return q("SELECT stop_id, stop_name, lat, lon FROM stops WHERE stop_name ILIKE %(p)s OR stop_id = %(s)s "
             "ORDER BY stop_name LIMIT 15", {"p": f"%{q_}%", "s": q_})


@app.get("/api/stops/near")
def stops_near(lat: float, lon: float, limit: int = 8):
    return q("""
        SELECT stop_id, stop_name, lat, lon,
               round((111000 * sqrt(power(lat - %(lat)s, 2) + power((lon - %(lon)s) * cos(radians(%(lat)s)), 2)))::numeric) AS meters
        FROM stops ORDER BY power(lat - %(lat)s, 2) + power((lon - %(lon)s) * cos(radians(%(lat)s)), 2) LIMIT %(n)s
    """, {"lat": lat, "lon": lon, "n": limit})


@app.get("/api/stops/{stop_id}/leave")
def leave_now(stop_id: str, walk_min: int = Query(5, ge=0, le=60)):
    """Next buses at a stop, adjusted by each bus's live delay (or the stop's usual lateness)."""
    stop = q("SELECT stop_id, stop_name, lat, lon FROM stops WHERE stop_id = %(s)s", {"s": stop_id})
    if not stop:
        raise HTTPException(404, "Unknown stop")
    now = datetime.now(timezone.utc)
    out = []
    for d, now_s, midnight in service_clock(now):
        rows = q("""
            SELECT st.trip_id, st.stop_sequence, st.arrival_s, t.route_id, r.route_short_name, t.headsign,
                   w.start_s,
                   live.vehicle_id, live.stop_sequence AS bus_seq, live.time AS bus_time,
                   ld.delay_s AS live_delay_s,
                   (SELECT 1 FROM stop_arrivals sa WHERE sa.trip_id = st.trip_id AND sa.stop_id = st.stop_id
                      AND sa.time > now() - interval '3 hours' LIMIT 1) AS already_passed,
                   hist.p80_delay_s AS usual_p80_delay_s
            FROM stop_times st
            JOIN trips t USING (trip_id)
            JOIN trip_windows w USING (trip_id)
            LEFT JOIN routes r ON r.route_id = t.route_id
            LEFT JOIN LATERAL (SELECT vehicle_id, stop_sequence, time FROM vehicle_positions vp
                               WHERE vp.trip_id = st.trip_id AND vp.time > now() - interval '3 minutes'
                               ORDER BY time DESC LIMIT 1) live ON TRUE
            LEFT JOIN LATERAL (SELECT delay_s FROM stop_arrivals sa WHERE sa.trip_id = st.trip_id
                               AND sa.time > now() - interval '3 hours' ORDER BY time DESC LIMIT 1) ld ON TRUE
            LEFT JOIN LATERAL (SELECT avg(p80_delay_s) AS p80_delay_s FROM stop_delay_hourly h
                               WHERE h.stop_id = st.stop_id AND h.route_id = t.route_id
                                 AND h.bucket > now() - interval '14 days'
                                 AND extract(hour FROM h.bucket AT TIME ZONE %(tz)s)
                                     = floor(st.arrival_s / 3600.0)::int %% 24) hist ON TRUE
            WHERE st.stop_id = %(stop)s
              AND t.service_id IN (SELECT service_id FROM active_services(%(d)s))
              AND st.arrival_s BETWEEN %(now_s)s - 1800 AND %(now_s)s + 5400
        """, {"stop": stop_id, "d": d, "now_s": now_s, "tz": str(TZ)})
        for r in rows:
            if r["already_passed"]:
                continue
            sched = midnight + timedelta(seconds=r["arrival_s"])
            if r["vehicle_id"]:
                delay, basis = (r["live_delay_s"] or 0), "live"
            elif now_s > r["start_s"] + 300:
                delay, basis = None, "ghost"      # should be on the road by now but isn't reporting
            else:
                delay, basis = (r["usual_p80_delay_s"] or 0), "history" if r["usual_p80_delay_s"] else "schedule"
            if delay is None:
                eta = None
            else:
                eta = sched + timedelta(seconds=delay)
                if eta < now - timedelta(minutes=1):
                    continue
            out.append({
                "route_id": r["route_id"], "route_short_name": r["route_short_name"], "headsign": r["headsign"],
                "trip_id": r["trip_id"], "scheduled": sched, "eta": eta, "basis": basis,
                "delay_min": round(delay / 60, 1) if delay is not None else None,
                "leave_at": eta - timedelta(minutes=walk_min) if eta else None,
                "stops_away": (r["stop_sequence"] - r["bus_seq"]) if r["bus_seq"] is not None else None,
            })
    out.sort(key=lambda x: x["eta"] or x["scheduled"])
    catchable = [x for x in out if x["leave_at"] and x["leave_at"] >= now]
    return {"stop": stop[0], "now": now, "walk_min": walk_min,
            "recommendation": catchable[0] if catchable else None, "arrivals": out[:12]}


# ── Under the hood: Tiger Data stats for the demo ────────────────────────────────
@app.get("/api/tiger")
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
