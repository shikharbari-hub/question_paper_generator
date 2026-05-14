const CACHE_NAME = 'questionai-v1';
const URLS_TO_CACHE = [
  '/',
  '/static/manifest.json',
];

// ── Install: cache karo ──
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => {
      return cache.addAll(URLS_TO_CACHE);
    })
  );
  self.skipWaiting();
});

// ── Activate: purana cache hatao ──
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(
        keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))
      )
    )
  );
  self.clients.claim();
});

// ── Fetch: network first, cache fallback ──
self.addEventListener('fetch', event => {
  // Sirf GET requests handle karo
  if (event.request.method !== 'GET') return;

  // Django POST (form submit) ko bypass karo
  const url = new URL(event.request.url);
  if (url.pathname === '/' && event.request.method === 'POST') return;

  event.respondWith(
    fetch(event.request)
      .then(response => {
        // Fresh response cache mein save karo
        const clone = response.clone();
        caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
        return response;
      })
      .catch(() => {
        // Network nahi hai toh cache se do
        return caches.match(event.request);
      })
  );
});