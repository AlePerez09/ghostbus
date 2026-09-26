# 👻 Ghost Bus

**Is my bus actually coming?** Ghost Bus tracks every Miami-Dade bus in real time and catches the three things that ruin a commute:

- **Ghost buses**: trips that are on the schedule but have no bus sending GPS.
- **Bunching**: two buses arriving within 3 minutes of each other, followed by a long gap.
- **Chronic lateness**: routes and stops that run late at the same hours every day.

It then gives riders one practical answer: **"leave in 4 minutes to catch the 8 at 11:57 PM."** That time comes from the bus's live position, or from how late that stop usually runs at that hour if the bus isn't on the road yet.

Built for ShellHacks 2026. **Target challenges:** MLH *Best Use of Tiger Data* (primary) and *Waymo Mobility Challenge*. See "Stretch goals" below for *Microsoft* and *ElevenLabs*.

---

## How it works

```
GTFS static (schedule) ──► load_static.py ──► routes / stops / trips / stop_times  (regular tables)
                                                            │
GTFS-realtime (every 15s) ─► poll_realtime.py ─► tracker.py ─┼─► vehicle_positions   (hypertable, compressed)
   or simulate.py (backup)                                  └─► stop_arrivals       (hypertable: headway + delay per stop)
                                                                       │
                                            continuous aggregates ◄────┘
                                            route_reliability_15m, stop_delay_hourly
                                                                       │
                                            api.py (FastAPI) ──► web/index.html (Leaflet map)
```

**How it uses Tiger Data (show this to the judges):**

| Feature | Where | Why it matters |
|---|---|---|
| Hypertables | `vehicle_positions` (1-hour chunks), `stop_arrivals` (1-day chunks) | Around 800 buses × 4 pings a minute is about 4.6M rows a day, and inserts and time-range queries stay fast |
| Compression | Policies in `db/schema.sql`, plus `python -m ghostbus.compress_now` | Older GPS data shrinks about 5×, shown live in the "Under the hood" tab |
| Continuous aggregates | `route_reliability_15m`, `stop_delay_hourly` | The leaderboard reads pre-computed 15-minute rollups instead of scanning raw events, and there's a benchmark in the UI |
| Real-time aggregation | `materialized_only = false` | Rollups include the last few minutes that haven't been materialized yet |
| Relational + time series | GTFS tables joined with hypertables in plain SQL | Ghost detection is one query that compares the schedule against the live feed |

**The core trick:** `tracker.py` works out *headway* (the time since the previous bus hit this stop) and *delay* (actual minus scheduled time) as each arrival is written. Continuous aggregates can't use window functions, so doing this at write time is what lets the bunching stats be pre-computed.

---

## Use it (riders)

Open the Ghost Bus link on your phone, then **Add to Home Screen**:
- **iPhone (Safari):** tap Share ⬆︎, then **Add to Home Screen**.
- **Android (Chrome):** tap **Install** in the app bar, or use menu ⋮, then **Install app**.

It opens full-screen like a normal app. **Near me** finds the stops around you and tells you when to leave, and **☆** saves stops like home or FIU. The page shell works offline, and data refreshes every 15 seconds.

## Run it (developers): one command

Needs Python 3.10+ and a Tiger Cloud service URL.

```bash
pip install -r requirements.txt
cp .env.example .env          # paste your Tiger Cloud URL into DATABASE_URL
python -m ghostbus            # open http://localhost:8000
```

On Windows you can just double-click **`Ghost Bus.bat`** instead.

On first boot against an empty database, the app does everything by itself: it creates the hypertables, compression and continuous aggregates, loads the Miami-Dade schedule (falling back to a built-in sample if the county site is down), generates 3 hours of history, and then keeps the bus feed running. It uses the realtime feed when `GTFS_RT_API_KEY` is set and simulated buses otherwise. The top-bar chip shows **Live** or **Demo data**, so it's always clear which one you're seeing.

**Only one server ever writes bus data.** A database lock makes sure of that. If the app runs on Render and on your laptop at the same time, the second copy waits on standby and just shows the data. If the first copy dies, the second takes over within a minute.

## Deploy it (public link for phones, about 5 minutes)

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/AlePerez09/ghostbus)

1. Click the button, sign in to Render, and paste your Tiger Cloud URL as `DATABASE_URL`. Leave `GTFS_RT_API_KEY` blank until Swiftly approves your key.
2. Render builds the app and gives you a URL like `https://ghostbus-xxxx.onrender.com`. That's the link riders install.
3. Render's free plan pauses apps after 15 idle minutes. Ghost Bus pings itself every 10 minutes (using `RENDER_EXTERNAL_URL`), so the feed keeps running.

Other hosts work too: use the included `Dockerfile`, which runs `python -m ghostbus` on `$PORT`.

<details><summary>Manual step-by-step (the individual scripts)</summary>


### 1. Get the code

```bash
git clone https://github.com/AlePerez09/ghostbus.git
cd ghostbus
python -m venv .venv
# Windows:  .venv\Scripts\activate      Mac/Linux:  source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Create the database on Tiger Cloud

1. Sign up at **tigerdata.com**. The MLH challenge page has the hackathon free-tier or credit details.
2. Create a service (a time-series service; the defaults are fine) and wait for it to show "Running."
3. Copy its **connection string** (service URL).
4. `cp .env.example .env` (on Windows: `copy .env.example .env`) and paste the string into `DATABASE_URL`.

### 3. Request the realtime API key now, because approval can take a while

Miami-Dade serves its live bus data through Swiftly. Fill out the request form linked on
[Miami-Dade's Open Data Feeds page](https://www.miamidade.gov/global/transportation/open-data-feeds.page).
When the key arrives, put it in `GTFS_RT_API_KEY` and check the exact vehicle-positions URL in Swiftly's docs.
**Until then, use the simulator (step 5b).** Everything else works the same way.

### 4. Create the tables and load the schedule

```bash
python -m ghostbus.setup_db          # hypertables, compression, continuous aggregates
python -m ghostbus.load_static       # downloads Miami-Dade's GTFS (about 1–2 min on Tiger Cloud)
```

If the county site is down or slow, use the fake Miami GTFS instead:
`python tools/fake_gtfs.py fake.zip && python -m ghostbus.load_static fake.zip`

### 5. Start collecting data. Leave this running all weekend.

```bash
# 5a. Real data (needs the API key):
python -m ghostbus.poll_realtime

# 5b. OR simulated data built from the real schedule (4 hours of history, then live):
python -m ghostbus.simulate --backfill 4 --live
```

### 6. Run the dashboard (in a second terminal)

```bash
uvicorn ghostbus.api:app --reload --port 8000
```

Open **http://localhost:8000**.

Before the demo, run `python -m ghostbus.compress_now --older-than 60` so the compression ratio shows up.

---

</details>

## How predictions work (in plain words)

1. **We watch the bus:** where it is and how late it's running right now.
2. **We remember:** how late buses usually are at *your* stop at this hour, learned from every past trip. This is a Tiger Data continuous aggregate of early, typical and late delays per stop, route and hour. The farther away the bus is, the more we lean on that history, because buses drift toward their usual pattern.
3. **We give a window, not a guess:** "10:57–11:02," narrow when the bus is close and wider when it's far. Your "leave" time aims at the early end, so the bus won't beat you there.
4. **We flag ghosts before they happen:** every scheduled trip is recorded as showed-up or not (`trip_outcomes`). If a trip missed several recent days, you'll see "Heads up: this trip didn't show up on 3 of the last 7 days."

Every answer carries **one confidence word** (Very likely / Likely / Rough estimate / May not come) and a **"Why?"** in one or two sentences.

**We correct ourselves.** Every 10 minutes the server looks at the last 6 hours of graded predictions. For each "how far ahead" band, it finds the window width that *would* have caught 80% of buses and moves halfway toward it, so windows widen when buses are unpredictable and tighten when they're steady (`learn.calibrate`).

**We grade ourselves.** Every 5 minutes the server predicts ahead for a sample of live buses using the same code the app uses (`predictions`), then matches each prediction to the real arrival (`prediction_results`). The Stats tab shows how often buses landed inside our window, rolled up by the `accuracy_hourly` continuous aggregate. Our goal is 80%, and in a backtest on simulated history the windows caught about 90%.

## Reliability, security and privacy

| Risk | What Ghost Bus does |
|---|---|
| Database or network drops | The feed reconnects by itself, waiting 5 s at first and up to 5 min. Web requests retry once, and dead connections are tested and replaced. |
| Two servers writing the same data | A Postgres advisory lock allows a single writer; the others stay on standby and take over automatically. |
| Live feed goes down | Ghost alerts pause (otherwise *every* bus would look like a ghost), and ETAs fall back to the schedule with a clear banner. |
| Schedule changes (new trip IDs) | The schedule reloads weekly at 3 AM, or as soon as its calendar expires. If the download fails, the old schedule stays. |
| County API rate limits | Polling backs off and follows `Retry-After`. |
| Database growing forever | Retention keeps raw GPS 14 days and stop events 90 days; the reliability rollups are kept. Old data is compressed. |
| Floods or scraping | Per-IP rate limit using Cloudflare's unforgeable `CF-Connecting-IP`, response caching, and a hard cap of 8 concurrent DB queries that answers "busy" quickly. |
| Injection or XSS | Parameterized SQL only, escaped output, and a strict Content-Security-Policy with no inline scripts. Also clickjacking protection, `nosniff` and HSTS. |
| Leaked secrets | `.env` is git-ignored, the push script refuses to upload it, Render stores `DATABASE_URL` as a secret, and error details never reach the public API. |
| Rider privacy | The server only receives your location rounded to about 100 m, and exact distances are computed on the phone. There are no access logs, no accounts and no analytics. Saved stops stay on the phone. |
| Vulnerable dependencies | Versions are pinned, and Dependabot opens PRs for security fixes. |

## API

| Endpoint | What it returns |
|---|---|
| `GET /api/live` | Latest position and delay for every bus heard from in the last 3 minutes |
| `GET /api/ghosts` | Trips that should be running now but haven't sent GPS in 10 minutes |
| `GET /api/reliability?hours=3` | Route leaderboard: score, % bunched, spacing regularity, average delay |
| `GET /api/routes/{id}/timeline?hours=6` | 15-minute buckets for one route |
| `GET /api/routes/{id}/shape` | Route line for the map |
| `GET /api/stops/search?q=` / `GET /api/stops/near?lat=&lon=` | Find stops |
| `GET /api/stops/{id}/leave?walk_min=5` | Next buses adjusted for delay, plus a "leave at" recommendation |
| `GET /api/health` | Feed status: phase, live or simulated, last update |
| `GET /api/tiger` | Hypertable sizes, compression ratio, and the raw-scan vs continuous-aggregate benchmark |

**Reliability score** = `100 × (1 − share of bunched arrivals) × (1 − headway CV ÷ 2)`, where CV is the standard deviation of the gaps between buses divided by their average. 100 means evenly spaced buses with none bunched.

---

## 3-minute demo script

1. **The problem (20s):** "You're at the stop. The app says the bus is coming. It never comes. That's a ghost bus."
2. **Live map (30s):** Show buses colored by lateness. Open the **Ghosts** tab and say how many scheduled trips have no bus right now.
3. **Leave now (40s):** Search a stop near FIU and show "Leave in X min," built from the bus's live delay.
4. **Routes (40s):** Show the worst route and its bunching chart, and explain what bunching is in one sentence.
5. **Under the hood (40s):** Walk through the hypertables, the compression ratio, and the continuous aggregate vs raw-scan timing. "That's all one Postgres database."
6. **Close (10s):** "Riders stop waiting for ghosts, and the county sees which routes to fix first."

---

## Splitting the work (team of 4)

- **Data:** Get the API key and the poller running against the real feed; check that trip_ids match the static GTFS.
- **SQL / Tiger:** Tune the aggregates, add a stop-level heatmap aggregate, and run the benchmark on the full-size data.
- **Frontend:** Polish the map and phone layout, and add a route filter.
- **Pitch:** Demo script, Devpost write-up, 3-minute video, and screenshots. Record the video early Sunday; submissions close at **11:00 AM**.

## Stretch goals

- **Microsoft "What's Missing?":** Qualifying needs AI built into the experience (not a chat window). One idea is to train a small model on `stop_arrivals` history to predict delay per stop and hour, replacing the p80 fallback.
- **ElevenLabs:** A spoken alert such as "Your 8 is running 6 minutes late, leave at 11:51."
- **Gemini:** A plain-English daily reliability report per route, generated from the aggregates.
- **Time-lapse:** Replay a route's day from `vehicle_positions` to show bunching form, which is a strong visual for Waymo.

## Known caveats

- Ghost detection needs realtime `trip_id`s that match the static GTFS. The API shows a warning if most trips look missing, which usually means a mismatch rather than an actual mass outage.
- The simulator is for testing and backup only. If you demo with it, say so.
- Headway and delay are measured when a bus passes a stop. If a bus skips several stops between pings, the arrival times in between are estimated.
