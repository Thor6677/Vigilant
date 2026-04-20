/**
 * Star map service worker.
 *
 * Only caches the three large, static JSON bundles under `/map/data/*.json`.
 * Everything else (API calls, HTML, Vite bundles) is passed through to the
 * network so fresh deploys and per-user data aren't cached.
 *
 * Registered from the map page only — see StarMap.tsx.
 */
const CACHE = 'vigilant-mapdata-v1';
const STATIC_PATHS = ['/map/data/systems.json', '/map/data/edges.json', '/map/data/regions.json'];

self.addEventListener('install', (e) => {
  self.skipWaiting();
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(STATIC_PATHS).catch(() => {})));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.filter(k => k !== CACHE).map(k => caches.delete(k))
    )).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (!url.pathname.startsWith('/map/data/')) return;   // don't touch other requests
  e.respondWith(
    caches.open(CACHE).then(async (cache) => {
      const cached = await cache.match(e.request);
      if (cached) {
        // stale-while-revalidate
        fetch(e.request).then(fresh => {
          if (fresh && fresh.ok) cache.put(e.request, fresh.clone());
        }).catch(() => {});
        return cached;
      }
      const fresh = await fetch(e.request);
      if (fresh && fresh.ok) cache.put(e.request, fresh.clone());
      return fresh;
    })
  );
});
