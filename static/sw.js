// yurit — Service Worker
// Strategy:
//   - Static assets (CSS, JS, fonts, icons from CDN + our static/): cache-first
//   - HTML pages: network-first, fallback to cache for offline mode
//   - API/POST: always network (don't cache mutations)

const CACHE_NAME = 'yurit-v5';
const STATIC_ASSETS = [
  '/static/img/icon-192.png',
  '/static/img/icon-512.png',
  '/static/img/icon-180.png',
  '/static/img/favicon.png',
  'https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css',
  'https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.1/font/bootstrap-icons.css',
  'https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js',
];

self.addEventListener('install', (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      // best-effort, never block install
      return Promise.all(
        STATIC_ASSETS.map((url) =>
          cache.add(url).catch((err) => console.warn('SW pre-cache failed:', url, err))
        )
      );
    })
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(
        names.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n))
      )
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return; // never cache POST/etc.

  const url = new URL(req.url);

  // Never cache Django admin or auth endpoints
  if (url.pathname.startsWith('/admin/') ||
      url.pathname.startsWith('/login') ||
      url.pathname.startsWith('/logout')) {
    return;
  }

  // Static assets (cache-first)
  const isStatic =
    url.pathname.startsWith('/static/') ||
    url.hostname.includes('cdn.jsdelivr.net') ||
    url.hostname.includes('jsdelivr.net');

  if (isStatic) {
    // stale-while-revalidate: keshdan tez qaytadi, fonda yangilanadi —
    // deploy'dan keyin JS/CSS o'zgarishlari keyingi ochilishda yetib boradi
    event.respondWith(
      caches.match(req).then((cached) => {
        const network = fetch(req).then((resp) => {
          if (resp.ok) {
            const respClone = resp.clone();
            caches.open(CACHE_NAME).then((c) => c.put(req, respClone));
          }
          return resp;
        }).catch(() => cached);
        return cached || network;
      })
    );
    return;
  }

  // POS lookup: stale-while-revalidate — repeat searches work offline,
  // online searches always refresh in background. Only the JSON GET endpoints.
  if (url.pathname === '/pos/lookup/') {
    event.respondWith(
      caches.match(req).then((cached) => {
        const network = fetch(req).then((resp) => {
          if (resp && resp.ok) {
            const respClone = resp.clone();
            caches.open(CACHE_NAME).then((c) => c.put(req, respClone));
          }
          return resp;
        }).catch(() => cached);
        return cached || network;
      })
    );
    return;
  }

  // HTML sahifalar: doim tarmoqdan. Kesh faqat POS sahifasi uchun
  // (offline savdo rejimi) — boshqa sahifalarni keshlash eski
  // ko'rinish/JS va login holati aralashuviga olib kelardi.
  if (req.headers.get('accept')?.includes('text/html')) {
    const isPos = url.pathname === '/pos/';
    event.respondWith(
      fetch(req)
        .then((resp) => {
          if (isPos && resp.ok && resp.type === 'basic' &&
              !resp.redirected) {
            const respClone = resp.clone();
            caches.open(CACHE_NAME).then((c) => c.put(req, respClone));
          }
          return resp;
        })
        .catch(() => (isPos ? caches.match(req) : Response.error()))
    );
  }
});
