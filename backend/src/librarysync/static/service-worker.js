// Bump the cache to ensure updated assets are served after UI changes.
const CACHE_NAME = "librarysync";
const CORE_ASSETS = [
  "/",
  "/login",
  "/add-watched",
  "/history",
  "/activity",
  "/settings",
  "/blacklist",
  "/offline",
  "/static/styles.css",
  "/static/core.js",
  "/static/status-utils.js",
  "/static/watch-utils.js",
  "/static/integrations-utils.js",
  "/static/page-home.js",
  "/static/page-login.js",
  "/static/page-settings.js",
  "/static/page-add-watched.js",
  "/static/page-history.js",
  "/static/page-activity.js",
  "/static/page-blacklist.js",
  "/static/chart.min.js",
  "/static/fonts/SpaceGrotesk-SemiBold.woff2",
  "/static/fonts/SpaceGrotesk-Regular.woff2",
  "/static/fonts/IBMPlexSans-Regular.woff2",
  "/static/fonts/IBMPlexSans-Medium.woff2",
  "/site.webmanifest",
  "/favicon.svg",
  "/favicon-96x96.png",
  "/favicon.ico",
  "/apple-touch-icon.png",
  "/web-app-manifest-192x192.png",
  "/web-app-manifest-512x512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) => cache.addAll(CORE_ASSETS))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.map((key) => (key === CACHE_NAME ? null : caches.delete(key)))),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") {
    return;
  }
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) {
    return;
  }
  if (url.pathname.startsWith("/api/")) {
    return;
  }
  if (
    url.pathname.startsWith("/static/") &&
    (url.pathname.endsWith(".js") || url.pathname.endsWith(".css"))
  ) {
    event.respondWith(
      fetch(event.request)
        .then((response) => {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
          return response;
        })
        .catch(() => caches.match(event.request)),
    );
    return;
  }

  if (event.request.mode === "navigate") {
    event.respondWith(
      fetch(event.request)
        .then((response) => {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
          return response;
        })
        .catch(() =>
          caches.match(event.request).then((cached) => cached || caches.match("/offline")),
        ),
    );
    return;
  }

  event.respondWith(
    caches.match(event.request).then((cached) => {
      const fetchPromise = fetch(event.request)
        .then((response) => {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
          return response;
        })
        .catch(() => cached);
      return cached || fetchPromise;
    }),
  );
});
