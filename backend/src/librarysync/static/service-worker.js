// Served from the site root (/service-worker.js) so its scope covers every page.
// The app registers it as /service-worker.js?v=<app version>, so each release gets a
// fresh cache automatically. Bump CACHE_SCHEMA when the caching logic itself changes.
const CACHE_SCHEMA = "v2";
const CACHE_PREFIX = "librarysync";
const CACHE_VERSION = new URL(self.location.href).searchParams.get("v") || "dev";
const CACHE_NAME = `${CACHE_PREFIX}-${CACHE_SCHEMA}-${CACHE_VERSION}`;
const OFFLINE_URL = "/offline";

// HTML pages served by the FastAPI app (see librarysync/main.py).
const PAGE_ROUTES = [
  "/",
  "/login",
  "/add-watched",
  "/history",
  "/watchlist",
  "/settings",
  "/stremio-addon",
  "/offline",
];

const CORE_ASSETS = [
  "/",
  "/login",
  "/add-watched",
  "/history",
  "/watchlist",
  "/settings",
  "/stremio-addon",
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
  "/static/page-watchlist.js",
  "/static/page-stremio-addon.js",
  "/static/page-watch-state.js",
  "/static/chart.min.js",
  "/static/icons/stremio.svg",
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

const ROOT_STATIC_ASSETS = new Set(
  CORE_ASSETS.filter((path) => !path.startsWith("/static/") && !PAGE_ROUTES.includes(path)),
);

function isCacheableResponse(response) {
  return Boolean(response && response.ok && response.type === "basic" && !response.redirected);
}

function putInCache(request, response) {
  if (!isCacheableResponse(response)) {
    return;
  }
  const copy = response.clone();
  caches
    .open(CACHE_NAME)
    .then((cache) => cache.put(request, copy))
    .catch(() => {
      // Ignore quota or storage errors; the network response is still returned.
    });
}

function matchCached(request) {
  // Pages request assets with a ?v=<version> cache-buster while the precache stores
  // the bare path, so fall back to a search-insensitive match.
  return caches
    .match(request)
    .then((cached) => cached || caches.match(request, { ignoreSearch: true }));
}

function orNetworkError(response) {
  return response || Response.error();
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) =>
        // Cache assets individually so a single missing file cannot abort the install.
        Promise.allSettled(
          CORE_ASSETS.map((asset) =>
            cache.add(new Request(asset, { cache: "reload" })).catch((error) => {
              console.warn(`service worker: failed to precache ${asset}`, error);
            }),
          ),
        ),
      )
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((key) => key.startsWith(CACHE_PREFIX) && key !== CACHE_NAME)
            .map((key) => caches.delete(key)),
        ),
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

  if (event.request.mode === "navigate") {
    const isAppPage = PAGE_ROUTES.includes(url.pathname);
    event.respondWith(
      fetch(event.request)
        .then((response) => {
          if (isAppPage) {
            putInCache(event.request, response);
          }
          return response;
        })
        .catch(() =>
          (isAppPage ? caches.match(event.request) : Promise.resolve(undefined))
            .then((cached) => cached || caches.match(OFFLINE_URL))
            .then(orNetworkError),
        ),
    );
    return;
  }

  if (
    url.pathname.startsWith("/static/") &&
    (url.pathname.endsWith(".js") || url.pathname.endsWith(".css"))
  ) {
    event.respondWith(
      fetch(event.request)
        .then((response) => {
          putInCache(event.request, response);
          return response;
        })
        .catch(() => matchCached(event.request).then(orNetworkError)),
    );
    return;
  }

  if (!url.pathname.startsWith("/static/") && !ROOT_STATIC_ASSETS.has(url.pathname)) {
    return;
  }

  event.respondWith(
    matchCached(event.request).then((cached) => {
      const fetchPromise = fetch(event.request)
        .then((response) => {
          putInCache(event.request, response);
          return response;
        })
        .catch(() => orNetworkError(cached));
      return cached || fetchPromise;
    }),
  );
});
