// Ghost Bus service worker: app shell works offline; live data always comes from the network.
const SHELL = 'ghostbus-shell-v6';
const DATA = 'ghostbus-data-v1';
const SHELL_FILES = [
  '/', '/manifest.webmanifest', '/static/app.js?v=7',
  '/static/vendor/leaflet/leaflet.js', '/static/vendor/leaflet/leaflet.css',
  '/static/icons/icon-192.png', '/static/icons/icon-512.png', '/static/icons/apple-touch-icon.png',
];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(SHELL_FILES)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => ![SHELL, DATA].includes(k)).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;   // map tiles etc. go straight to network

  if (url.pathname.startsWith('/api/')) {
    // Network first; if offline, show the last data we had (the UI marks it as stale).
    e.respondWith(fetch(e.request).then(res => {
      if (res.ok) { const copy = res.clone(); caches.open(DATA).then(c => c.put(e.request, copy)); }
      return res;
    }).catch(() => caches.match(e.request)));
    return;
  }
  if (e.request.mode === 'navigate' || url.pathname === '/sw.js') {
    // The page itself: always try for the newest version, fall back to the cached one offline.
    e.respondWith(fetch(e.request).then(res => {
      const copy = res.clone(); caches.open(SHELL).then(c => c.put('/', copy)); return res;
    }).catch(() => caches.match('/')));
    return;
  }
  // Static files (Leaflet, icons): serve cached copy instantly, refresh it in the background.
  e.respondWith(caches.match(e.request).then(cached => {
    const fresh = fetch(e.request).then(res => {
      if (res.ok) { const copy = res.clone(); caches.open(SHELL).then(c => c.put(e.request, copy)); }
      return res;
    }).catch(() => cached);
    return cached || fresh;
  }));
});
