"""What Ghost Bus learns from history, and how it keeps score of itself.

Tables (all Tiger Data hypertables) and rollups:
  delay_profile_hourly  (continuous aggregate)  typical early/usual/late delay per stop, route and hour
  route_profile_hourly  (continuous aggregate)  same per route, used when a stop has little history
  trip_outcomes         did each scheduled trip actually show up?  -> "ghost risk"
  predictions           a sample of our own predictions, logged when made
  prediction_results    each prediction next to what really happened
  accuracy_hourly       (continuous aggregate)  hit rates by how far ahead we predicted
"""
from datetime import datetime, timedelta, timezone

from .config import TZ
from .predict import horizon_band, predict

# Every statement is idempotent: safe to run on every boot.
SCHEMA = [
    # ── ghost history ──
    """CREATE TABLE IF NOT EXISTS trip_outcomes (
         time TIMESTAMPTZ NOT NULL, trip_id TEXT NOT NULL, route_id TEXT, direction_id INT,
         start_s INT, seen BOOLEAN)""",
    "SELECT create_hypertable('trip_outcomes', by_range('time', INTERVAL '7 days'), if_not_exists => TRUE)",
    "CREATE UNIQUE INDEX IF NOT EXISTS trip_outcomes_uniq ON trip_outcomes (trip_id, time)",
    "CREATE INDEX IF NOT EXISTS trip_outcomes_key ON trip_outcomes (route_id, direction_id, start_s, time DESC)",
    # ── self-grading ──
    """CREATE TABLE IF NOT EXISTS predictions (
         made_at TIMESTAMPTZ NOT NULL, trip_id TEXT, stop_id TEXT, route_id TEXT, horizon_band INT,
         live BOOLEAN, predicted TIMESTAMPTZ, lo TIMESTAMPTZ, hi TIMESTAMPTZ, resolved BOOLEAN DEFAULT FALSE)""",
    "SELECT create_hypertable('predictions', by_range('made_at', INTERVAL '1 day'), if_not_exists => TRUE)",
    "CREATE INDEX IF NOT EXISTS predictions_open ON predictions (made_at DESC) WHERE NOT resolved",
    """CREATE TABLE IF NOT EXISTS prediction_results (
         time TIMESTAMPTZ NOT NULL, made_at TIMESTAMPTZ, route_id TEXT, stop_id TEXT, horizon_band INT,
         live BOOLEAN, error_s INT, in_window BOOLEAN)""",
    "SELECT create_hypertable('prediction_results', by_range('time', INTERVAL '1 day'), if_not_exists => TRUE)",
    "CREATE INDEX IF NOT EXISTS sa_trip_stop ON stop_arrivals (trip_id, stop_id, time DESC)",
    "CREATE INDEX IF NOT EXISTS vp_trip_time ON vehicle_positions (trip_id, time DESC)",
    # ── rollups ──
    """CREATE MATERIALIZED VIEW IF NOT EXISTS delay_profile_hourly WITH (timescaledb.continuous) AS
       SELECT time_bucket(INTERVAL '1 hour', time) AS bucket, route_id, stop_id, count(*) AS n,
              percentile_cont(0.1) WITHIN GROUP (ORDER BY delay_s) AS p10,
              percentile_cont(0.5) WITHIN GROUP (ORDER BY delay_s) AS p50,
              percentile_cont(0.9) WITHIN GROUP (ORDER BY delay_s) AS p90
       FROM stop_arrivals WHERE delay_s IS NOT NULL GROUP BY 1, 2, 3 WITH NO DATA""",
    """CREATE MATERIALIZED VIEW IF NOT EXISTS route_profile_hourly WITH (timescaledb.continuous) AS
       SELECT time_bucket(INTERVAL '1 hour', time) AS bucket, route_id, count(*) AS n,
              percentile_cont(0.1) WITHIN GROUP (ORDER BY delay_s) AS p10,
              percentile_cont(0.5) WITHIN GROUP (ORDER BY delay_s) AS p50,
              percentile_cont(0.9) WITHIN GROUP (ORDER BY delay_s) AS p90
       FROM stop_arrivals WHERE delay_s IS NOT NULL GROUP BY 1, 2 WITH NO DATA""",
    """CREATE MATERIALIZED VIEW IF NOT EXISTS accuracy_hourly WITH (timescaledb.continuous) AS
       SELECT time_bucket(INTERVAL '1 hour', time) AS bucket, route_id, horizon_band, live,
              count(*) AS n,
              count(*) FILTER (WHERE abs(error_s) <= 120) AS within_2min,
              count(*) FILTER (WHERE in_window) AS in_window,
              avg(abs(error_s))::float AS avg_abs_error_s
       FROM prediction_results GROUP BY 1, 2, 3, 4 WITH NO DATA""",
]
for _v, _start in (("delay_profile_hourly", "15 days"), ("route_profile_hourly", "15 days"), ("accuracy_hourly", "3 days")):
    SCHEMA += [
        f"SELECT add_continuous_aggregate_policy('{_v}', start_offset => INTERVAL '{_start}', "
        f"end_offset => INTERVAL '1 minute', schedule_interval => INTERVAL '10 minutes', if_not_exists => TRUE)",
        f"ALTER MATERIALIZED VIEW {_v} SET (timescaledb.materialized_only = false)",
    ]
SCHEMA += [
    "SELECT add_retention_policy('predictions', INTERVAL '14 days', if_not_exists => TRUE)",
    "SELECT add_retention_policy('prediction_results', INTERVAL '90 days', if_not_exists => TRUE)",
    "SELECT add_retention_policy('trip_outcomes', INTERVAL '120 days', if_not_exists => TRUE)",
]
NEW_VIEWS = ("delay_profile_hourly", "route_profile_hourly", "accuracy_hourly")


def migrate(conn):
    """conn must be autocommit (continuous aggregates can't be created inside a transaction)."""
    existed = {v: conn.execute("SELECT to_regclass(%s) IS NOT NULL", (v,)).fetchone()[0] for v in NEW_VIEWS}
    for stmt in SCHEMA:
        conn.execute(stmt)
    for v, was_there in existed.items():
        if not was_there:      # learn from history we already have, right away
            conn.execute(f"CALL refresh_continuous_aggregate('{v}', now() - INTERVAL '15 days', now())")


# ── lookups (batched: one query no matter how many buses) ───────────────────────
def profiles(run, keys):
    """keys: iterable of (stop_id, route_id, local_hour) -> {key: {n, p10, p50, p90}}.
    Uses the stop's own history, or the route's when the stop has fewer than 5 samples."""
    keys = list(set(keys))
    if not keys:
        return {}
    stops, routes, hours = [k[0] for k in keys], [k[1] for k in keys], [k[2] for k in keys]
    rows = run("""
        WITH k AS (SELECT * FROM unnest(%(s)s::text[], %(r)s::text[], %(h)s::int[]) AS k(stop_id, route_id, h)),
        sp AS (
          SELECT k.stop_id, k.route_id, k.h, sum(p.n) AS n,
                 sum(p.p10 * p.n) / nullif(sum(p.n), 0) AS p10, sum(p.p50 * p.n) / nullif(sum(p.n), 0) AS p50,
                 sum(p.p90 * p.n) / nullif(sum(p.n), 0) AS p90
          FROM k LEFT JOIN delay_profile_hourly p
            ON p.stop_id = k.stop_id AND p.route_id = k.route_id AND p.bucket > now() - INTERVAL '14 days'
           AND extract(hour FROM p.bucket AT TIME ZONE %(tz)s) = k.h
          GROUP BY 1, 2, 3),
        rp AS (
          SELECT k.route_id, k.h, sum(p.n) AS n,
                 sum(p.p10 * p.n) / nullif(sum(p.n), 0) AS p10, sum(p.p50 * p.n) / nullif(sum(p.n), 0) AS p50,
                 sum(p.p90 * p.n) / nullif(sum(p.n), 0) AS p90
          FROM (SELECT DISTINCT route_id, h FROM k) k LEFT JOIN route_profile_hourly p
            ON p.route_id = k.route_id AND p.bucket > now() - INTERVAL '14 days'
           AND extract(hour FROM p.bucket AT TIME ZONE %(tz)s) = k.h
          GROUP BY 1, 2),
        ra AS (   -- last resort: the route at any hour
          SELECT p.route_id, sum(p.n) AS n,
                 sum(p.p10 * p.n) / nullif(sum(p.n), 0) AS p10, sum(p.p50 * p.n) / nullif(sum(p.n), 0) AS p50,
                 sum(p.p90 * p.n) / nullif(sum(p.n), 0) AS p90
          FROM route_profile_hourly p
          WHERE p.route_id IN (SELECT DISTINCT route_id FROM k) AND p.bucket > now() - INTERVAL '14 days'
          GROUP BY 1)
        SELECT sp.stop_id, sp.route_id, sp.h,
               CASE WHEN coalesce(sp.n, 0) >= 5 THEN sp.n WHEN coalesce(rp.n, 0) >= 5 THEN rp.n ELSE ra.n END AS n,
               CASE WHEN coalesce(sp.n, 0) >= 5 THEN sp.p10 WHEN coalesce(rp.n, 0) >= 5 THEN rp.p10 ELSE ra.p10 END AS p10,
               CASE WHEN coalesce(sp.n, 0) >= 5 THEN sp.p50 WHEN coalesce(rp.n, 0) >= 5 THEN rp.p50 ELSE ra.p50 END AS p50,
               CASE WHEN coalesce(sp.n, 0) >= 5 THEN sp.p90 WHEN coalesce(rp.n, 0) >= 5 THEN rp.p90 ELSE ra.p90 END AS p90
        FROM sp LEFT JOIN rp ON rp.route_id = sp.route_id AND rp.h = sp.h
                LEFT JOIN ra ON ra.route_id = sp.route_id
    """, {"s": stops, "r": routes, "h": hours, "tz": str(TZ)})
    out = {}
    for r in rows:
        r = dict(r) if not isinstance(r, dict) else r
        if r["n"]:
            out[(r["stop_id"], r["route_id"], r["h"])] = {k: float(r[k]) for k in ("n", "p10", "p50", "p90")}
    return out


def ghost_risks(run, keys):
    """keys: iterable of (route_id, direction_id, start_s) -> {key: (missed, total)} over the last 28 days."""
    keys = list(set(keys))
    if not keys:
        return {}
    rows = run("""
        SELECT o.route_id, o.direction_id, o.start_s, count(*) AS total, count(*) FILTER (WHERE NOT o.seen) AS missed
        FROM trip_outcomes o
        JOIN unnest(%(r)s::text[], %(d)s::int[], %(s)s::int[]) AS k(route_id, direction_id, start_s)
          ON o.route_id = k.route_id AND o.direction_id = k.direction_id AND o.start_s = k.start_s
        WHERE o.time > now() - INTERVAL '28 days'
        GROUP BY 1, 2, 3
    """, {"r": [k[0] for k in keys], "d": [k[1] for k in keys], "s": [k[2] for k in keys]})
    return {(r["route_id"], r["direction_id"], r["start_s"]): (r["missed"], r["total"]) for r in rows}


def risk_from(days):
    """Ghost risk as a fraction, only when we've seen the trip at least 3 times."""
    if not days or days[1] < 3:
        return None
    return days[0] / days[1]


def local_hour(sched_dt):
    return sched_dt.astimezone(TZ).hour


# ── background jobs (run by the single data-writing server) ─────────────────────
def _dict_run(conn):
    def run(sql, params=None):
        cur = conn.execute(sql, params or {})
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    return run


def record_trip_outcomes(conn, lookback_s=1800):
    """For trips that should have finished recently: did a bus ever report for them?
    Trips whose start or end fell in a feed outage are skipped (outages don't count as ghosts).
    Built to be cheap: ONE pass over recent GPS pings gives both 'which trips were seen' and
    'which 5-minute slots had live data', then the schedule is checked against those two sets."""
    now = datetime.now(timezone.utc)
    local = now.astimezone(TZ)
    since = now - timedelta(seconds=lookback_s + 7200)
    pings = conn.execute("""
        SELECT array_agg(DISTINCT trip_id) FILTER (WHERE trip_id IS NOT NULL),
               array_agg(DISTINCT time_bucket(INTERVAL '5 minutes', time))
        FROM vehicle_positions WHERE time > %s
    """, (since,)).fetchone()
    seen, alive = set(pings[0] or []), set(pings[1] or [])
    rows = []
    for d in (local.date(), local.date() - timedelta(days=1)):
        midnight = datetime(d.year, d.month, d.day, 12, tzinfo=TZ) - timedelta(hours=12)
        now_s = int((now - midnight).total_seconds())
        due = conn.execute("""
            SELECT trip_id, route_id, direction_id, start_s, end_s FROM trip_windows
            WHERE service_id IN (SELECT service_id FROM active_services(%s))
              AND end_s + 600 BETWEEN %s AND %s
        """, (d, now_s - lookback_s, now_s)).fetchall()
        for trip_id, route_id, direction_id, start_s, end_s in due:
            start = midnight + timedelta(seconds=start_s)
            end = midnight + timedelta(seconds=end_s)
            slot = lambda t: t - timedelta(minutes=t.minute % 5, seconds=t.second, microseconds=t.microsecond)
            if slot(start) not in alive or slot(end) not in alive:
                continue
            rows.append((start, trip_id, route_id, direction_id, start_s, trip_id in seen))
    if rows:
        with conn.cursor() as cur:
            cur.executemany("""INSERT INTO trip_outcomes (time, trip_id, route_id, direction_id, start_s, seen)
                               VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (trip_id, time) DO NOTHING""", rows)
    conn.commit()
    return len(rows)


def record_predictions(conn):
    """Every few minutes, predict ahead for a sample of live buses (3, 8, 15 and 25 minutes out)
    using exactly the same model the app uses, and log it so we can grade ourselves later."""
    run = _dict_run(conn)
    rows = run("""
        WITH live AS (
          SELECT DISTINCT ON (vehicle_id) vehicle_id, trip_id, stop_sequence, time
          FROM vehicle_positions WHERE time > now() - INTERVAL '2 minutes' AND trip_id IS NOT NULL
          ORDER BY vehicle_id, time DESC),
        cur AS (
          SELECT l.trip_id, l.stop_sequence AS seq, st.arrival_s AS cur_s,
                 (SELECT delay_s FROM stop_arrivals sa WHERE sa.trip_id = l.trip_id
                    AND sa.time > now() - INTERVAL '3 hours' ORDER BY sa.time DESC LIMIT 1) AS delay_s
          FROM live l JOIN stop_times st ON st.trip_id = l.trip_id AND st.stop_sequence = l.stop_sequence),
        tgt AS (
          SELECT c.*, h.target_min, t.route_id, t.direction_id, w.start_s,
                 nx.stop_id AS target_stop, nx.stop_sequence AS target_seq, nx.arrival_s AS target_s
          FROM cur c CROSS JOIN (VALUES (3), (8), (15), (25)) AS h(target_min)
          JOIN trips t ON t.trip_id = c.trip_id JOIN trip_windows w ON w.trip_id = c.trip_id
          JOIN LATERAL (SELECT st.stop_id, st.stop_sequence, st.arrival_s FROM stop_times st
                        WHERE st.trip_id = c.trip_id AND st.arrival_s - c.cur_s >= h.target_min * 60
                        ORDER BY st.stop_sequence LIMIT 1) nx ON TRUE
          WHERE c.delay_s IS NOT NULL)
        SELECT * FROM tgt
    """)
    if not rows:
        return 0
    now = datetime.now(timezone.utc)
    local = now.astimezone(TZ)
    cand = []
    for r in rows:
        stop_id, seq, arr_s = r["target_stop"], r["target_seq"], r["target_s"]
        # service day: pick the one where the bus's current stop time is closest to now
        best = None
        for d in (local.date(), local.date() - timedelta(days=1)):
            mid = datetime(d.year, d.month, d.day, 12, tzinfo=TZ) - timedelta(hours=12)
            cur_sched = mid + timedelta(seconds=r["cur_s"])
            gap = abs((now - cur_sched).total_seconds() - (r["delay_s"] or 0))
            if best is None or gap < best[0]:
                best = (gap, mid)
        mid = best[1]
        sched = mid + timedelta(seconds=arr_s)
        cand.append((r, stop_id, seq, sched, (arr_s - r["cur_s"]) / 60))
    profs = profiles(run, [(c[1], c[0]["route_id"], local_hour(c[3])) for c in cand])
    ghosts = ghost_risks(run, [(c[0]["route_id"], c[0]["direction_id"], c[0]["start_s"]) for c in cand])
    out = []
    for r, stop_id, seq, sched, mins in cand:
        days = ghosts.get((r["route_id"], r["direction_id"], r["start_s"]))
        p = predict(live=True, current_delay_s=r["delay_s"], minutes_away=mins, stops_away=seq - r["seq"],
                    profile=profs.get((stop_id, r["route_id"], local_hour(sched))),
                    ghost_risk=risk_from(days), ghost_days=days)
        out.append((now, r["trip_id"], stop_id, r["route_id"], horizon_band(mins), True,
                    sched + timedelta(seconds=p.delay_s), sched + timedelta(seconds=p.lo_s),
                    sched + timedelta(seconds=p.hi_s)))
    with conn.cursor() as cur:
        with cur.copy("COPY predictions (made_at, trip_id, stop_id, route_id, horizon_band, live, predicted, lo, hi)"
                      " FROM STDIN") as cp:
            for row in out:
                cp.write_row(row)
    conn.commit()
    return len(out)


def resolve_predictions(conn):
    """Match logged predictions with the bus's real arrival and store how far off we were."""
    cur = conn.execute("""
        WITH done AS (
          UPDATE predictions p SET resolved = TRUE
          FROM stop_arrivals a
          WHERE NOT p.resolved AND p.made_at > now() - INTERVAL '4 hours'
            AND a.trip_id = p.trip_id AND a.stop_id = p.stop_id
            AND a.time BETWEEN p.made_at - INTERVAL '5 minutes' AND p.made_at + INTERVAL '3 hours'
          RETURNING a.time AS actual, p.*)
        INSERT INTO prediction_results (time, made_at, route_id, stop_id, horizon_band, live, error_s, in_window)
        SELECT actual, made_at, route_id, stop_id, horizon_band, live,
               extract(epoch FROM actual - predicted)::int, actual BETWEEN lo AND hi
        FROM done
    """)
    conn.commit()
    return cur.rowcount or 0
