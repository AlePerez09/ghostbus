// ───────────────────────── helpers ─────────────────────────
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const fmtTime = d => d ? new Date(d).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }) : '—';
const minsFromNow = d => Math.round((new Date(d) - Date.now()) / 60000);
const store = {
  get(k, d) { try { const v = localStorage.getItem('gb:' + k); return v ? JSON.parse(v) : d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem('gb:' + k, JSON.stringify(v)); } catch {} },
};
let lastOk = 0, offline = false;
async function api(p) {
  try {
    const r = await fetch(p);
    if (!r.ok) throw new Error(r.status);
    lastOk = Date.now(); offline = false;
    return r.json();
  } catch (e) { offline = true; renderStatus(); throw e; }
}
function delayColor(s) {
  if (s == null) return css('--muted');
  if (s < 120) return css('--good'); if (s < 300) return css('--warning');
  if (s < 600) return css('--serious'); return css('--critical');
}
function delayText(s) {
  if (s == null) return 'no delay data yet';
  const m = Math.round(Math.abs(s) / 60);
  if (m < 1) return 'on time';
  return s < 0 ? `${m} min early` : `${m} min late`;
}
const BASIS = { live: 'tracking the bus live', history: 'bus not out yet · padded for usual delays', schedule: 'schedule only', ghost: 'not reporting' };

// ───────────────────────── map ─────────────────────────
const map = L.map('map', { zoomControl: false, attributionControl: true }).setView([25.765, -80.30], 12);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '&copy; OpenStreetMap', maxZoom: 19, className: 'dark-tiles' }).addTo(map);
const busLayer = L.layerGroup().addTo(map), routeLayer = L.layerGroup().addTo(map),
      stopLayer = L.layerGroup().addTo(map), meLayer = L.layerGroup().addTo(map);

async function refreshBuses() {
  const buses = await api('/api/live');
  busLayer.clearLayers();
  for (const b of buses) {
    L.circleMarker([b.lat, b.lon], { radius: 6, weight: 2, color: css('--surface'), fillColor: delayColor(b.delay_s), fillOpacity: 1 })
      .bindPopup(`<b>Route ${esc(b.route_short_name || b.route_id)}</b><br>${delayText(b.delay_s)}<br><span class="muted">Bus ${esc(b.vehicle_id)} · ${fmtTime(b.time)}</span>`)
      .addTo(busLayer);
  }
}

map.on('click', async e => {
  const near = await api(`/api/stops/near?lat=${coarse(e.latlng.lat)}&lon=${coarse(e.latlng.lng)}&limit=5`);
  const tap = { lat: e.latlng.lat, lon: e.latlng.lng };
  const best = near.map(s => ({ ...s, m: metersBetween(tap, s) })).sort((a, b) => a.m - b.m)[0];
  if (best && best.m < 400) openStop(best.stop_id);
});

// ───────────────────────── status chip + install ─────────────────────────
let health = null;
async function refreshHealth() { try { health = await api('/api/health'); } catch {} renderStatus(); }
function renderStatus() {
  const dot = $('#status .dot'), txt = $('#status-text');
  if (offline) { dot.style.background = css('--critical'); txt.textContent = 'Offline · last data'; return; }
  if (!health) return;
  const ago = lastOk ? ` · ${Math.round((Date.now() - lastOk) / 1000)}s ago` : '';
  if (health.feed_fresh === null) { dot.style.background = css('--warning'); txt.textContent = 'Starting up…'; return; }
  if (health.feed_fresh === false) { dot.style.background = css('--serious'); txt.textContent = 'Bus data delayed'; return; }
  const sim = health.mode === 'sim';
  dot.style.background = sim ? css('--warning') : css('--good');
  txt.textContent = (sim ? 'Demo data' : 'Live') + ago;
}
setInterval(renderStatus, 1000);

let deferredInstall = null;
window.addEventListener('beforeinstallprompt', e => { e.preventDefault(); deferredInstall = e; $('#install').style.display = 'grid'; });
$('#install').onclick = async () => { if (!deferredInstall) return; deferredInstall.prompt(); await deferredInstall.userChoice; deferredInstall = null; $('#install').style.display = 'none'; };
const standalone = matchMedia('(display-mode: standalone)').matches || navigator.standalone;
if (/iphone|ipad|ipod/i.test(navigator.userAgent) && !standalone && !store.get('iosTipDone', false)) {
  $('#ios-tip').style.display = 'block';
  $('#ios-tip').onclick = () => { $('#ios-tip').style.display = 'none'; store.set('iosTipDone', true); };
}
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});

// ───────────────────────── sheet + tabs ─────────────────────────
const sheet = $('#sheet');
function setSheet(state) {
  sheet.classList.toggle('full', state === 'full'); sheet.classList.toggle('mini', state === 'mini');
  document.body.classList.toggle('sheet-full', state === 'full'); document.body.classList.toggle('sheet-mini', state === 'mini');
}
let dragY = null;
$('#handle').addEventListener('pointerdown', e => { dragY = e.clientY; });
window.addEventListener('pointerup', e => {
  if (dragY == null) return;
  const dy = e.clientY - dragY; dragY = null;
  const full = sheet.classList.contains('full'), mini = sheet.classList.contains('mini');
  if (Math.abs(dy) < 8) setSheet(full ? 'half' : 'full');                // tap toggles
  else if (dy < 0) setSheet(mini ? 'half' : 'full');
  else setSheet(full ? 'half' : 'mini');
});

let currentTab = 'near';
function showTab(name) {
  currentTab = name;
  document.querySelectorAll('nav.tabs button').forEach(b => b.classList.toggle('on', b.dataset.p === name || (name === 'stop' && b.dataset.p === 'near')));
  document.querySelectorAll('.panel').forEach(p => p.classList.toggle('on', p.id === 'p-' + name));
  if (sheet.classList.contains('mini')) setSheet('half');
  if (name === 'saved') renderSaved();
  if (name === 'ghosts') refreshGhosts().catch(() => {});
  if (name === 'routes') refreshRoutes().catch(() => {});
  if (name === 'stats') refreshStats().catch(() => {});
}
document.querySelectorAll('nav.tabs button').forEach(b => b.onclick = () => { openStopId = null; showTab(b.dataset.p); });

// ───────────────────────── near me + search ─────────────────────────
let myPos = null;
function locate() {
  if (!navigator.geolocation) { $('#near-out').innerHTML = '<div class="empty">Location isn’t available on this device. Search for a stop instead.</div>'; return; }
  $('#near-out').innerHTML = '<div class="empty">Finding you…</div>';
  navigator.geolocation.getCurrentPosition(p => {
    myPos = { lat: p.coords.latitude, lon: p.coords.longitude };
    meLayer.clearLayers();
    L.marker([myPos.lat, myPos.lon], { icon: L.divIcon({ className: '', html: '<div class="me"></div>', iconSize: [16, 16] }) }).addTo(meLayer);
    map.setView([myPos.lat, myPos.lon], 15);
    store.set('locOk', true);
    renderNear();
  }, err => {
    $('#near-out').innerHTML = `<div class="empty">Couldn’t get your location (${esc(err.message)}). Search for a stop instead.</div>
      <button class="btn ghost" id="near-retry">Try again</button>`;
    $('#near-retry').onclick = locate;
  }, { enableHighAccuracy: true, timeout: 10000, maximumAge: 60000 });
}
$('#near-btn').onclick = locate;
$('#locate').onclick = () => { showTab('near'); locate(); };
const walkMin = m => Math.max(1, Math.ceil(m / 80));     // ~80 m per minute walking
// Privacy: the server only ever sees your location rounded to ~100 m. Exact distances are computed here.
const coarse = x => x.toFixed(3);
function metersBetween(a, b) {
  const R = 6371000, r = Math.PI / 180, dLat = (b.lat - a.lat) * r, dLon = (b.lon - a.lon) * r;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * r) * Math.cos(b.lat * r) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}

async function renderNear() {
  if (!myPos) return;
  const stops = (await api(`/api/stops/near?lat=${coarse(myPos.lat)}&lon=${coarse(myPos.lon)}&limit=8`))
    .map(s => ({ ...s, meters: metersBetween(myPos, s) })).sort((a, b) => a.meters - b.meters);
  if (!stops.length) { $('#near-out').innerHTML = '<div class="empty">No stops found nearby.</div>'; return; }
  const far = stops[0].meters > 3000;
  const top = stops.slice(0, 4);
  const results = await Promise.all(top.map(s => api(`/api/stops/${encodeURIComponent(s.stop_id)}/leave?walk_min=${walkMin(s.meters)}`).catch(() => null)));
  $('#near-out').innerHTML = (far ? '<div class="warn">You’re a long way from the nearest stop. Showing the closest ones anyway.</div>' : '') +
    top.map((s, i) => stopRow(s, results[i], walkMin(s.meters))).join('') +
    `<button class="btn ghost" id="near-refresh">↻ Refresh</button>`;
  $('#near-refresh').onclick = renderNear;
  bindStopRows('#near-out');
}

function stopRow(stop, leave, walk) {
  const r = leave && leave.recommendation;
  let right = '<span class="muted">No bus soon</span>';
  if (r) {
    const m = minsFromNow(r.leave_at);
    right = `<div class="big ${m <= 0 ? 'go' : ''}">${m <= 0 ? 'Go now' : m + ' min'}</div><div class="muted">leave · route ${esc(r.route_short_name || r.route_id)}</div>`;
  }
  const dist = walk != null ? `${walk} min walk · ` : '';
  const nextBuses = leave ? leave.arrivals.filter(a => a.eta).slice(0, 3).map(a => `<span class="badge" style="min-width:0;font-size:12px">${esc(a.route_short_name || a.route_id)} ${fmtTime(a.eta)}</span>`).join(' ') : '';
  return `<div class="row" data-stop="${esc(stop.stop_id)}">
    <div class="grow"><div class="ellipsis">${esc(stop.stop_name)}</div>
      <div class="sub">${dist}stop ${esc(stop.stop_id)}</div>
      <div style="margin-top:5px;display:flex;gap:4px;flex-wrap:wrap">${nextBuses}</div></div>
    <div class="right">${right}</div></div>`;
}
function bindStopRows(sel) { document.querySelectorAll(sel + ' [data-stop]').forEach(r => r.onclick = () => openStop(r.dataset.stop)); }

let searchTimer = null;
$('#stop-q').addEventListener('input', e => {
  clearTimeout(searchTimer);
  const v = e.target.value.trim();
  if (v.length < 2) { $('#search-results').innerHTML = ''; return; }
  searchTimer = setTimeout(async () => {
    const stops = await api('/api/stops/search?q=' + encodeURIComponent(v));
    $('#search-results').innerHTML = stops.length ? stops.map(s => `<div class="row" data-stop="${esc(s.stop_id)}"><div class="grow ellipsis">${esc(s.stop_name)}<div class="sub">Stop ${esc(s.stop_id)}</div></div><span class="muted">›</span></div>`).join('') : '<div class="empty">No matching stops.</div>';
    bindStopRows('#search-results');
  }, 220);
});

// ───────────────────────── stop detail ─────────────────────────
let openStopId = null;
const saved = () => store.get('saved', []);
function toggleSaved(stop, walk) {
  const list = saved(), i = list.findIndex(s => s.id === stop.stop_id);
  if (i >= 0) list.splice(i, 1); else list.push({ id: stop.stop_id, name: stop.stop_name });
  if (walk != null && store.get('walk:' + stop.stop_id, null) == null) store.set('walk:' + stop.stop_id, walk);
  store.set('saved', list);
}

async function openStop(id, focus = true) {
  openStopId = id;
  if (focus) { showTab('stop'); $('#p-stop').innerHTML = '<div class="empty">Loading…</div>'; $('#search-results').innerHTML = ''; }
  const walkSel = store.get('walk:' + id, null);
  let walk = walkSel;
  if (walk == null && myPos) {
    const info = await api(`/api/stops/${encodeURIComponent(id)}/leave?walk_min=5`).catch(() => null);
    walk = info ? walkMin(metersBetween(myPos, info.stop)) : 5;
  }
  walk = walk ?? 5;
  const d = await api(`/api/stops/${encodeURIComponent(id)}/leave?walk_min=${walk}`);
  if (openStopId !== id) return;
  stopLayer.clearLayers();
  L.circleMarker([d.stop.lat, d.stop.lon], { radius: 10, color: css('--accent'), weight: 3, fillOpacity: 0 }).addTo(stopLayer);
  if (focus) map.setView([d.stop.lat, d.stop.lon], Math.max(map.getZoom(), 15));
  const isSaved = saved().some(s => s.id === id);
  const r = d.recommendation;
  let html = `<button class="back" id="stop-back">‹ Back</button>
    <div class="stophead"><div class="grow"><div class="big" style="font-size:18px">${esc(d.stop.stop_name)}</div><div class="muted">Stop ${esc(id)}</div></div>
    <button class="star ${isSaved ? 'on' : ''}" id="star" aria-label="Save stop">${isSaved ? '★' : '☆'}</button></div>
    <div class="sub" style="margin-top:8px">Walk time to stop:
      <select class="walk" id="walk">${[1,2,3,4,5,6,8,10,12,15,20].map(n => `<option ${n === walk ? 'selected' : ''}>${n}</option>`).join('')}</select> min</div>`;
  if (d.live_ok === false) html += '<div class="warn">Live bus tracking is delayed right now. These are scheduled times, padded for usual delays.</div>';
  if (r) {
    const m = minsFromNow(r.leave_at);
    html += `<div class="card rec"><div class="sub">${m <= 0 ? 'Leave' : 'Leave in'}</div>
      <div class="huge ${m <= 0 ? 'go' : ''}">${m <= 0 ? 'Now!' : m + ' min'}</div>
      <div style="margin-top:4px">to catch route <b>${esc(r.route_short_name || r.route_id)}</b> at <b>${fmtTime(r.eta)}</b>
      ${r.delay_min ? `<span class="sub">(${delayText(r.delay_min * 60)})</span>` : ''}</div>
      <div class="tag" style="margin-top:6px"><span class="dot" style="background:${r.basis === 'live' ? css('--good') : css('--muted')}"></span>${BASIS[r.basis]}${r.stops_away != null ? ` · ${r.stops_away} stops away` : ''}</div></div>`;
  } else html += '<div class="card">No catchable bus in the next 90 minutes.</div>';
  html += '<h3>Next buses</h3><table class="t">' + (d.arrivals.map(a => `<tr>
      <td style="width:60px"><span class="badge">${esc(a.route_short_name || a.route_id)}</span></td>
      <td class="sub">${a.basis === 'ghost' ? '<span style="color:var(--critical)">👻 not reporting</span>' : esc(BASIS[a.basis])}${a.delay_min && a.basis === 'live' ? ` · ${delayText(a.delay_min * 60)}` : ''}</td>
      <td>${a.eta ? fmtTime(a.eta) : '<s class="muted">' + fmtTime(a.scheduled) + '</s>'}</td></tr>`).join('') || '<tr><td class="empty">No buses scheduled soon.</td></tr>') + '</table>';
  $('#p-stop').innerHTML = html;
  $('#stop-back').onclick = () => { openStopId = null; stopLayer.clearLayers(); showTab('near'); };
  $('#star').onclick = () => { toggleSaved(d.stop, walk); openStop(id, false); };
  $('#walk').onchange = e => { store.set('walk:' + id, +e.target.value); openStop(id, false); };
}

// ───────────────────────── saved stops ─────────────────────────
async function renderSaved() {
  const list = saved();
  if (!list.length) { $('#p-saved').innerHTML = '<div class="empty">No saved stops yet.<br>Open a stop and tap ☆ to keep it here, like your stop at home or at FIU.</div>'; return; }
  $('#p-saved').innerHTML = list.map(s => stopRow({ stop_id: s.id, stop_name: s.name }, null, null)).join('');
  bindStopRows('#p-saved');
  const results = await Promise.all(list.map(s => api(`/api/stops/${encodeURIComponent(s.id)}/leave?walk_min=${store.get('walk:' + s.id, 5)}`).catch(() => null)));
  if (currentTab !== 'saved') return;
  $('#p-saved').innerHTML = list.map((s, i) => stopRow({ stop_id: s.id, stop_name: s.name }, results[i], store.get('walk:' + s.id, 5))).join('');
  bindStopRows('#p-saved');
}

// ───────────────────────── ghosts ─────────────────────────
async function refreshGhosts() {
  const g = await api('/api/ghosts');
  $('#n-ghost').textContent = g.ghost_count;
  if (currentTab !== 'ghosts') return;
  let html = g.warning ? `<div class="warn">${esc(g.warning)}</div>` : '';
  if (g.feed_down) { $('#p-ghosts').innerHTML = html; return; }
  html += `<div class="sub">${g.scheduled_now} trips should be running right now. <b>${g.ghost_count}</b> ${g.ghost_count === 1 ? 'hasn’t' : 'haven’t'} sent a GPS signal in 10 minutes — anyone waiting for ${g.ghost_count === 1 ? 'it is' : 'them is'} waiting for a ghost.</div>`;
  if (!g.ghosts.length) html += '<div class="empty">No ghosts right now 🎉</div>';
  html += g.ghosts.map(t => `<div class="row" data-route="${esc(t.route_id)}"><span class="badge">${esc(t.route_short_name || t.route_id)}</span>
      <div class="grow"><div class="ellipsis">${esc(t.headsign || 'Trip ' + t.trip_id)}</div>
      <div class="sub">Should have left ${t.minutes_since_start} min ago · ${t.minutes_left} min of service missing</div></div></div>`).join('');
  $('#p-ghosts').innerHTML = html;
  document.querySelectorAll('#p-ghosts [data-route]').forEach(r => r.onclick = () => showRoute(r.dataset.route));
}

// ───────────────────────── routes ─────────────────────────
async function refreshRoutes() {
  const rows = await api('/api/reliability?hours=3');
  $('#route-out').innerHTML = rows.map(r => `
    <div class="row" data-route="${esc(r.route_id)}">
      <span class="badge">${esc(r.route_short_name || r.route_id)}</span>
      <div class="grow"><div class="ellipsis">${esc(r.route_long_name || '')}</div>
        <div class="sub">${r.bunched_pct}% bunched · every ${Math.round((r.avg_headway_s || 0) / 60)} min · ${delayText(r.avg_delay_s)} avg</div>
        <div class="bar"><i style="width:${r.score}%"></i></div></div>
      <span class="big" style="font-size:16px">${r.score}</span>
    </div>`).join('') || '<div class="empty">Not enough data yet — check back in a few minutes.</div>';
  document.querySelectorAll('#route-out [data-route]').forEach(r => r.onclick = () => showRoute(r.dataset.route));
}

async function showRoute(routeId) {
  routeLayer.clearLayers();
  const shapes = await api(`/api/routes/${encodeURIComponent(routeId)}/shape`);
  const lines = shapes.map(c => L.polyline(c, { color: css('--accent'), weight: 4, opacity: .85 }).addTo(routeLayer));
  if (lines.length) map.fitBounds(L.featureGroup(lines).getBounds(), { padding: [30, 30] });
  const tl = await api(`/api/routes/${encodeURIComponent(routeId)}/timeline?hours=6`);
  if (currentTab !== 'routes') showTab('routes');
  $('#route-detail').innerHTML = `<h3>Route ${esc(routeId)}: bunched arrivals per 15 min (last 6h)</h3>` + barChart(tl);
  $('#route-detail').scrollIntoView({ behavior: 'smooth' });
}

function barChart(rows) {
  if (!rows.length) return '<div class="empty">No data yet.</div>';
  const W = 360, H = 130, pad = { l: 26, b: 20, t: 8 };
  const max = Math.max(1, ...rows.map(r => r.bunched));
  const bw = (W - pad.l) / rows.length;
  let s = `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="Bunched arrivals per 15 minutes">`;
  s += `<line x1="${pad.l}" x2="${W}" y1="${H - pad.b}" y2="${H - pad.b}" stroke="${css('--line')}"/>`;
  s += `<text x="${pad.l - 4}" y="${pad.t + 8}" text-anchor="end">${max}</text><text x="${pad.l - 4}" y="${H - pad.b}" text-anchor="end">0</text>`;
  rows.forEach((r, i) => {
    const h = (H - pad.b - pad.t) * r.bunched / max, x = pad.l + i * bw + 1;
    const tip = `${fmtTime(r.bucket)} · ${r.bunched} bunched of ${r.arrivals} arrivals · ${delayText(r.avg_delay_s)} avg`;
    s += `<rect x="${x}" y="${pad.t}" width="${Math.max(1, bw - 2)}" height="${H - pad.b - pad.t}" fill="transparent" data-tip="${esc(tip)}"/>`;
    if (h > 0) s += `<rect x="${x}" y="${H - pad.b - h}" width="${Math.max(1, bw - 2)}" height="${h}" rx="2" fill="${css('--accent')}" pointer-events="none"/>`;
    if (i % 8 === 0) s += `<text x="${x}" y="${H - 5}">${fmtTime(r.bucket)}</text>`;
  });
  return s + '</svg>';
}
document.addEventListener('pointermove', e => {
  const t = e.target.closest && e.target.closest('[data-tip]'), tip = $('#hovertip');
  if (!t) { tip.style.display = 'none'; return; }
  tip.textContent = t.dataset.tip; tip.style.display = 'block';
  tip.style.left = Math.min(e.clientX + 12, innerWidth - 220) + 'px'; tip.style.top = (e.clientY + 12) + 'px';
});

// ───────────────────────── stats (Tiger Data) ─────────────────────────
async function refreshStats() {
  const t = await api('/api/tiger');
  const n = x => x == null ? '—' : Number(x).toLocaleString();
  let html = '<div class="sub">Everything runs on one Tiger Data (PostgreSQL + TimescaleDB) database: schedule tables and GPS time series side by side.</div>';
  for (const [name, s] of Object.entries(t.hypertables)) {
    html += `<h3>${name} <span class="muted">hypertable</span></h3><table class="t">
      <tr><td>Rows</td><td>${n(s.rows)}</td></tr>
      <tr><td>Chunks (compressed)</td><td>${s.chunks} (${s.compressed_chunks})</td></tr>
      <tr><td>Size on disk</td><td>${(s.bytes / 1e6).toFixed(1)} MB</td></tr>
      <tr><td>Compression ratio</td><td>${s.compression_ratio ? s.compression_ratio + '×' : 'pending'}</td></tr></table>`;
  }
  const b = t.benchmark_24h_leaderboard;
  html += `<h3>24h leaderboard query</h3><table class="t">
    <tr><td>Scanning raw arrivals</td><td>${b.raw_scan_ms} ms</td></tr>
    <tr><td>Continuous aggregate</td><td>${b.continuous_aggregate_ms} ms</td></tr>
    <tr><td>Speed-up</td><td>${(b.raw_scan_ms / Math.max(b.continuous_aggregate_ms, 0.01)).toFixed(1)}×</td></tr></table>
    <div class="legend">
      <span class="tag"><span class="dot" style="background:var(--good)"></span>On time (&lt;2 min)</span>
      <span class="tag"><span class="dot" style="background:var(--warning)"></span>2–5 late</span>
      <span class="tag"><span class="dot" style="background:var(--serious)"></span>5–10 late</span>
      <span class="tag"><span class="dot" style="background:var(--critical)"></span>10+ late</span>
      <span class="tag"><span class="dot" style="background:var(--muted)"></span>No data yet</span>
    </div>`;
  $('#p-stats').innerHTML = html;
}

// ───────────────────────── refresh loop ─────────────────────────
async function refreshAll() {
  await Promise.allSettled([refreshHealth(), refreshBuses(), refreshGhosts()]);
  if (openStopId && currentTab === 'stop') openStop(openStopId, false).catch(() => {});
  if (currentTab === 'routes') refreshRoutes().catch(() => {});
}
refreshAll();
setInterval(() => { if (!document.hidden) refreshAll(); }, 15000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshAll(); });

// Returning users who already allowed location get their nearby stops right away.
if (store.get('locOk', false)) locate();
else if (saved().length) showTab('saved');
