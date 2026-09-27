# Ghost Bus video script

About 2 min 50 s. Read slowly and pause between scenes.

---

## 1 · 0:00–0:15
**Screen:** map zoomed in on one Miami stop

You check the app, and it says your bus is four minutes away. Twenty minutes later, it still hasn't shown up. Riders call that a ghost bus. We built Ghost Bus to catch them before you're the one waiting.

---

## 2 · 0:15–0:40
**Screen:** zoom out to Miami-Dade, Broward and Palm Beach

Ghost Bus follows every bus across Miami-Dade, Broward and Palm Beach, hundreds of buses at a time. For this demo it runs on a simulated feed built from each county's real published schedule, because Miami-Dade's live GPS key hasn't arrived yet. When it does, the same pipeline switches straight to live data.

---

## 3 · 0:40–1:15
**Screen:** search your stop, open it, point at leave time, window, confidence word

Most apps tell you the bus is seven minutes out. Ghost Bus tells you when to leave. It adds your walk, then gives a window for when the bus should really arrive, and says how sure it is in plain words, like Very likely, Rough estimate, or May not come. If the bus looks like a ghost, it gives you a backup bus instead.

---

## 4 · 1:15–1:35
**Screen:** Ghosts tab, scroll slowly

The Ghosts tab lists every trip that should be on the road right now but hasn't sent a GPS signal in ten minutes. Those are the buses people are standing and waiting for that aren't coming.

---

## 5 · 1:35–2:05
**Screen:** Stats tab, hold on the numbers

We didn't want to just guess. Every five minutes, Ghost Bus writes down its own predictions, and when the bus actually arrives, it grades itself. If the bus keeps landing outside the window, the window widens, and if it's too cautious, it tightens. It has graded over 170,000 of its own predictions, and the bus landed inside the window 82 percent of the time, right on our goal of eight out of ten.

---

## 6 · 2:05–2:40
**Screen:** Tiger Cloud tab, run the query, point at Route 100, then Palm Tran 3 (re-run first; read the new numbers if they changed)

Everything runs on Tiger Data. Every GPS ping goes into a TimescaleDB hypertable. Continuous aggregates roll those pings into fifteen-minute reliability stats on their own, and compression and retention keep months of history cheap. This one query checks the last three hours in a tenth of a second. Route 100 had buses bunched together 305 times, and Palm Tran 3 left riders waiting over half an hour 759 times, so it covers Palm Beach too.

---

## 7 · 2:40–3:00
**Screen:** back to the app, click Install, show it open as its own window

Ghost Bus installs straight from the browser, with no app store and no account. It's free, it's live on the web today, and it works for riders across three counties. Ghost Bus: know when to leave, and know when your bus isn't coming.
