// Auto Loot Claimer Service Worker
// Ensures standalone app capabilities while strictly maintaining 100% live synchronization.

const CACHE_NAME = 'autoloot-shell-v1';
const STATIC_ASSETS = [
  '/',
  '/manifest.json',
  '/static/icons/icon.svg',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/favicon.ico'
];

// Install: Cache essential shell assets
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(STATIC_ASSETS);
    }).then(() => self.skipWaiting())
  );
});

// Activate: Clean up older caches and claim clients immediately
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys.map((key) => {
          if (key !== CACHE_NAME) {
            return caches.delete(key);
          }
        })
      );
    }).then(() => self.clients.claim())
  );
});

// Fetch: Strict synchronization policy
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // 1. ALL API endpoints are strictly NETWORK ONLY. Never serve cached API responses.
  // This guarantees the installed app and the website are always 100% in sync with the backend.
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(fetch(event.request));
    return;
  }

  // 2. Navigation requests: Network first, falling back to cache if offline
  if (event.request.mode === 'navigate') {
    event.respondWith(
      fetch(event.request).catch(() => {
        return caches.match(event.request).then((response) => {
          return response || caches.match('/');
        });
      })
    );
    return;
  }

  // 3. Static assets: Stale-while-revalidate for fast app launches
  event.respondWith(
    caches.match(event.request).then((cached) => {
      const networked = fetch(event.request).then((response) => {
        if (response && response.status === 200 && response.type === 'basic') {
          const toCache = response.clone();
          caches.open(CACHE_NAME).then((cache) => {
            cache.put(event.request, toCache);
          });
        }
        return response;
      }).catch(() => cached);
      return cached || networked;
    })
  );
});

// Listen for broadcast sync signals between app windows
self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'SYNC_NOW') {
    self.clients.matchAll().then((clients) => {
      clients.forEach((client) => {
        client.postMessage({ type: 'SYNC_TRIGGERED' });
      });
    });
  }
});
