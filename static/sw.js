// Service worker — makes the app installable as a PWA.
// IMPORTANT: never serve a cached HTML page. The app is in active development and the shell
// changes constantly; a cached "/" would show a stale/broken UI. Navigations always go to the
// network. We only cache static icons/manifest, and only fall back to cache if the network is
// genuinely unreachable.
const CACHE = 'ccchat-static-v5';
const STATIC = ['/manifest.json', '/icon-192.png', '/icon-512.png'];

self.addEventListener('install', e => {
  e.waitUntil((async () => {
    const c = await caches.open(CACHE);
    await Promise.allSettled(STATIC.map(u => c.add(u)));
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', e => {
  e.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/ws')) return;  // always live
  // proxied sub-apps (in-browser VS Code, terminal) own their own caching/SW — never intercept them
  if (url.pathname.startsWith('/code/') || url.pathname.startsWith('/term/')) return;
  // navigations (the HTML page) — network only, never cache, so the UI is never stale
  if (req.mode === 'navigate' || url.pathname === '/' || url.pathname.endsWith('.html')) {
    e.respondWith(fetch(req));
    return;
  }
  // static assets — network-first, fall back to cache only if offline
  e.respondWith(
    fetch(req).then(resp => {
      if (resp.ok && url.origin === location.origin && STATIC.includes(url.pathname)) {
        const copy = resp.clone();
        caches.open(CACHE).then(c => c.put(req, copy)).catch(() => {});
      }
      return resp;
    }).catch(() => caches.match(req))
  );
});
