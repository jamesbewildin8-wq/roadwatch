/* Three policies, because the assets behave differently.
 *
 * SHELL - app code, icons, camera dataset. Cache-first, permanently. This is
 *         what lets the app open and work with no signal at all.
 * TILES - base map imagery, cached as you view it, capped. Roads you've driven
 *         stay usable offline.
 * NEVER - /api/* and traffic tiles. Both are live claims about the world right
 *         now. A cached dwell alert points at a road where nothing is stopped;
 *         a cached traffic tile shows congestion that cleared an hour ago.
 *         Both look current, which is what makes them worse than nothing.
 */
const SHELL_CACHE = "roadwatch-shell-v4";
const TILE_CACHE  = "roadwatch-tiles-v1";
const TILE_MAX    = 400;

const ASSETS = [
  "./", "./index.html", "./trip.js", "./leaflet.js", "./leaflet.css",
  "./cameras.geojson", "./manifest.webmanifest",
  "./icon-180.png", "./icon-192.png", "./icon-512.png",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL_CACHE)
    .then((c) => Promise.allSettled(ASSETS.map((u) => c.add(u))))
    .then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys()
    .then((ks) => Promise.all(ks
      .filter((k) => k !== SHELL_CACHE && k !== TILE_CACHE)
      .map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

async function trimTiles(){
  const c = await caches.open(TILE_CACHE);
  const keys = await c.keys();
  if (keys.length > TILE_MAX){
    await Promise.all(keys.slice(0, keys.length - TILE_MAX).map((k) => c.delete(k)));
  }
}

self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET") return;
  const url = new URL(e.request.url);

  // Live data: never cached, no fallback. An honest failure beats a stale claim.
  if (url.hostname.endsWith("api.tomtom.com")) return;
  if (url.origin === self.location.origin && /(^|\/)api\//.test(url.pathname)) return;

  if (/basemaps\.cartocdn\.com|tile\.openstreetmap\.org/.test(url.hostname)){
    e.respondWith(caches.match(e.request).then((hit) => hit ||
      fetch(e.request).then((r) => {
        if (r.ok){
          const copy = r.clone();
          caches.open(TILE_CACHE).then((c) => c.put(e.request, copy).then(trimTiles));
        }
        return r;
      }).catch(() => new Response("", { status: 504 }))));
    return;
  }

  if (url.origin !== self.location.origin) return;

  e.respondWith(caches.match(e.request).then((hit) => hit ||
    fetch(e.request).then((r) => {
      if (r.ok){
        const copy = r.clone();
        caches.open(SHELL_CACHE).then((c) => c.put(e.request, copy));
      }
      return r;
    }).catch(() => caches.match("./index.html"))));
});
