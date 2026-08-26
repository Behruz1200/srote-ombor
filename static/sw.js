// yurit — Service Worker
// Strategy:
//   - Static assets (CSS, JS, fonts, icons from CDN + our static/): cache-first
//   - HTML pages: network-first, fallback to cache for offline mode
//   - API/POST: always network (don't cache mutations)

const CACHE_NAME = 'yurit-v213';
// SEC-2: kutubxonalar endi o'zimizda (static/vendor/) — CDN emas. Bu yerда
// oldindan keshlamaymiz (WhiteNoise hash'li nom beradi); /static/ so'rovlari
// runtime'да cache-first bo'lib avtomat keshlanadi (pastдаgi handler).
const STATIC_ASSETS = [
  '/static/img/icon-192.png',
  '/static/img/icon-512.png',
  '/static/img/icon-180.png',
  '/static/img/favicon.png',
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

  // POS lookup: NETWORK-FIRST (SW-2). Online bo'lsa DOIM yangi narx/qoldiq
  // olinadi — kesh faqat offline zaxira. Ilgari kesh birinchi kelib, narx
  // o'zgargandan keyingi birinchi skan ESKI narxда sotardi.
  if (url.pathname === '/pos/lookup/') {
    event.respondWith(
      fetch(req).then((resp) => {
        // SW-1: FAQAT haqiqiy JSON javobni keshlaymiz. Sessiya tugab login
        // sahifasiga (HTML) redirect qilsa — keshga tushmasin.
        const ct = (resp && resp.headers.get('content-type')) || '';
        if (resp && resp.ok && resp.type === 'basic' && !resp.redirected &&
            ct.indexOf('application/json') !== -1) {
          const respClone = resp.clone();
          caches.open(CACHE_NAME).then((c) => c.put(req, respClone));
        }
        return resp;
      }).catch(() =>
        // tarmoq yo'q — keshdagi eski javob, u ham bo'lmasa JSON 503 (SW-3)
        caches.match(req).then((cached) => cached || new Response(
          JSON.stringify({ found: false, offline: true }),
          { status: 503, headers: { 'Content-Type': 'application/json' } })))
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
