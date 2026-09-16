// Drop Scout – Service Worker (PWA offline shell)
const CACHE_NAME = 'dropscout-v2';
const SHELL_URLS = ['/', '/manifest.json'];

// Install: cache the app shell
self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_URLS))
  );
  self.skipWaiting();
});

// Activate: purge old caches
self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// Fetch: network-first for API, cache-first for shell
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);

  // Always go to network for API calls and POST requests
  if (url.pathname.startsWith('/api/') || e.request.method !== 'GET') {
    return;
  }

  // For app shell: try network first, fall back to cache
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        // Update cache with fresh copy
        const clone = res.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(e.request, clone));
        return res;
      })
      .catch(() => caches.match(e.request))
  );
});
