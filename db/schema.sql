-- Ghost Bus schema for Tiger Data (TimescaleDB on PostgreSQL)
-- Relational GTFS tables + time-series hypertables side by side in one database.

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ─────────────────────────── Static GTFS (relational) ───────────────────────────
CREATE TABLE IF NOT EXISTS routes (
    route_id         TEXT PRIMARY KEY,
    route_short_name TEXT,
    route_long_name  TEXT,
    route_type       INT,
<<<<<<< HEAD
    route_color      TEXT
=======
    route_color      TEXT,
    agency           TEXT DEFAULT 'mdt'
>>>>>>> f967724 (Added Palm Beach, Broward, and Monroe Counties)
);

CREATE TABLE IF NOT EXISTS stops (
    stop_id   TEXT PRIMARY KEY,
    stop_name TEXT,
    lat       DOUBLE PRECISION,
    lon       DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS trips (
    trip_id      TEXT PRIMARY KEY,
    route_id     TEXT,
    service_id   TEXT,
    direction_id INT,
    headsign     TEXT,
<<<<<<< HEAD
    shape_id     TEXT
=======
    shape_id     TEXT,
    agency       TEXT DEFAULT 'mdt'
>>>>>>> f967724 (Added Palm Beach, Broward, and Monroe Counties)
);
CREATE INDEX IF NOT EXISTS trips_route_idx ON trips (route_id, direction_id);

-- Times are stored as seconds after the service day's midnight (GTFS allows > 24:00:00).
CREATE TABLE IF NOT EXISTS stop_times (
    trip_id       TEXT,
    stop_sequence INT,
    stop_id       TEXT,
    arrival_s     INT,
    departure_s   INT,
    PRIMARY KEY (trip_id, stop_sequence)
);
CREATE INDEX IF NOT EXISTS stop_times_stop_idx ON stop_times (stop_id);

CREATE TABLE IF NOT EXISTS calendar (
    service_id TEXT PRIMARY KEY,
    monday BOOL, tuesday BOOL, wednesday BOOL, thursday BOOL,
    friday BOOL, saturday BOOL, sunday BOOL,
    start_date DATE, end_date DATE
);

CREATE TABLE IF NOT EXISTS calendar_dates (
    service_id     TEXT,
    date           DATE,
    exception_type INT,           -- 1 = service added, 2 = service removed
    PRIMARY KEY (service_id, date)
);

CREATE TABLE IF NOT EXISTS shapes (
    shape_id TEXT,
    seq      INT,
    lat      DOUBLE PRECISION,
    lon      DOUBLE PRECISION,
    PRIMARY KEY (shape_id, seq)
);

-- One row per trip: when it is scheduled to start and finish (filled by load_static.py).
CREATE TABLE IF NOT EXISTS trip_windows (
    trip_id  TEXT PRIMARY KEY,
    route_id TEXT,
    service_id TEXT,
    direction_id INT,
    start_s  INT,
<<<<<<< HEAD
    end_s    INT
=======
    end_s    INT,
    agency   TEXT DEFAULT 'mdt'
>>>>>>> f967724 (Added Palm Beach, Broward, and Monroe Counties)
);

-- Service IDs running on a given date, honoring calendar + calendar_dates exceptions.
CREATE OR REPLACE FUNCTION active_services(d DATE) RETURNS TABLE (service_id TEXT)
LANGUAGE sql STABLE AS $$
    SELECT c.service_id FROM calendar c
    WHERE d BETWEEN c.start_date AND c.end_date
      AND CASE extract(isodow FROM d)::int
            WHEN 1 THEN c.monday WHEN 2 THEN c.tuesday WHEN 3 THEN c.wednesday
            WHEN 4 THEN c.thursday WHEN 5 THEN c.friday WHEN 6 THEN c.saturday
            ELSE c.sunday END
      AND NOT EXISTS (SELECT 1 FROM calendar_dates x
                      WHERE x.service_id = c.service_id AND x.date = d AND x.exception_type = 2)
    UNION
    SELECT x.service_id FROM calendar_dates x WHERE x.date = d AND x.exception_type = 1
$$;

-- ─────────────────────────── Time series (hypertables) ───────────────────────────
-- Raw GPS pings: one row per bus every poll (~15 s). This is the high-volume table.
CREATE TABLE IF NOT EXISTS vehicle_positions (
    time          TIMESTAMPTZ NOT NULL,
    vehicle_id    TEXT NOT NULL,
    route_id      TEXT,
    trip_id       TEXT,
    direction_id  INT,
    lat           DOUBLE PRECISION,
    lon           DOUBLE PRECISION,
    bearing       REAL,
    speed_mps     REAL,
    stop_sequence INT,
    status        TEXT
);
SELECT create_hypertable('vehicle_positions', by_range('time', INTERVAL '1 hour'), if_not_exists => TRUE);
CREATE UNIQUE INDEX IF NOT EXISTS vp_uniq ON vehicle_positions (vehicle_id, time);
CREATE INDEX IF NOT EXISTS vp_route_time ON vehicle_positions (route_id, time DESC);

-- Derived events: a bus reached a stop. Headway and delay are computed at write time,
-- so continuous aggregates can roll them up without window functions.
CREATE TABLE IF NOT EXISTS stop_arrivals (
    time          TIMESTAMPTZ NOT NULL,
    route_id      TEXT NOT NULL,
    direction_id  INT,
    stop_id       TEXT NOT NULL,
    vehicle_id    TEXT,
    trip_id       TEXT,
    headway_s     INT,          -- seconds since the previous bus on this route+direction hit this stop
    delay_s       INT           -- actual minus scheduled arrival (positive = late)
);
SELECT create_hypertable('stop_arrivals', by_range('time', INTERVAL '1 day'), if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS sa_stop_time ON stop_arrivals (stop_id, time DESC);
CREATE INDEX IF NOT EXISTS sa_route_time ON stop_arrivals (route_id, direction_id, time DESC);

-- ─────────────────────────── Compression ───────────────────────────
ALTER TABLE vehicle_positions SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'route_id, vehicle_id',
    timescaledb.compress_orderby   = 'time DESC'
);
SELECT add_compression_policy('vehicle_positions', INTERVAL '6 hours', if_not_exists => TRUE);

ALTER TABLE stop_arrivals SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'route_id, stop_id',
    timescaledb.compress_orderby   = 'time DESC'
);
SELECT add_compression_policy('stop_arrivals', INTERVAL '2 days', if_not_exists => TRUE);

-- ─────────────────────────── Continuous aggregates ───────────────────────────
-- Route reliability every 15 minutes: the dashboard reads this instead of scanning raw rows.
CREATE MATERIALIZED VIEW IF NOT EXISTS route_reliability_15m
WITH (timescaledb.continuous) AS
SELECT
    time_bucket(INTERVAL '15 minutes', time) AS bucket,
    route_id,
    direction_id,
    count(*)                                         AS arrivals,
    count(headway_s)                                 AS headways,
    avg(headway_s)::float                            AS avg_headway_s,
    stddev_samp(headway_s)::float                    AS sd_headway_s,
    count(*) FILTER (WHERE headway_s < 180)          AS bunched,      -- two buses < 3 min apart
    count(*) FILTER (WHERE headway_s > 1800)         AS long_gaps,    -- riders waited 30+ min
    avg(delay_s)::float                              AS avg_delay_s,
    percentile_cont(0.8) WITHIN GROUP (ORDER BY delay_s) AS p80_delay_s
FROM stop_arrivals
GROUP BY 1, 2, 3
WITH NO DATA;

SELECT add_continuous_aggregate_policy('route_reliability_15m',
    start_offset => INTERVAL '3 days', end_offset => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute', if_not_exists => TRUE);

-- Per stop and hour of day: how late buses usually are here (feeds the "leave now" buffer).
CREATE MATERIALIZED VIEW IF NOT EXISTS stop_delay_hourly
WITH (timescaledb.continuous) AS
SELECT
    time_bucket(INTERVAL '1 hour', time) AS bucket,
    stop_id,
    route_id,
    count(*)                                              AS arrivals,
    avg(delay_s)::float                                   AS avg_delay_s,
    percentile_cont(0.8) WITHIN GROUP (ORDER BY delay_s)  AS p80_delay_s
FROM stop_arrivals
GROUP BY 1, 2, 3
WITH NO DATA;

SELECT add_continuous_aggregate_policy('stop_delay_hourly',
    start_offset => INTERVAL '7 days', end_offset => INTERVAL '1 minute',
    schedule_interval => INTERVAL '5 minutes', if_not_exists => TRUE);

-- Real-time aggregation: include not-yet-materialized recent rows in query results.
ALTER MATERIALIZED VIEW route_reliability_15m SET (timescaledb.materialized_only = false);
ALTER MATERIALIZED VIEW stop_delay_hourly     SET (timescaledb.materialized_only = false);
