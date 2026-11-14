// Family Table service worker. Deliberately tiny: the app always talks to the server.
// Its one job is a friendly "can't reach Family Table" screen instead of a blank page
// when the phone has no connection (an installed app has no browser reload button).
// It never stores recipes, shopping lists, signed-in pages, or anything sent back by the server.
"use strict";

const VERSION = "__VERSION__";                // filled in by the server; changes when the files below do
const CACHE = `family-table-offline-${VERSION}`;
const OFFLINE_FILES = ["__OFFLINE_FILES__"];  // public files only (offline page, its CSS and icons)
const OFFLINE_PAGE = OFFLINE_FILES[0];
const OFFLINE_PATHS = new Set(OFFLINE_FILES.map((url) => new URL(url, self.location).pathname));

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(OFFLINE_FILES)).then(() => self.skipWaiting()));
});

// Remove offline files from older versions.
self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith("family-table-") && k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;   // form posts and saves always go straight to the server

  // Pages: always from the network. Only if the network itself fails, show the offline page.
  if (req.mode === "navigate") {
    event.respondWith(fetch(req).catch(() => caches.match(OFFLINE_PAGE)));
    return;
  }

  // The offline page's own CSS and icons: network first, saved copy only when offline.
  const url = new URL(req.url);
  if (url.origin === self.location.origin && OFFLINE_PATHS.has(url.pathname)) {
    event.respondWith(fetch(req).catch(() => caches.match(req, { ignoreSearch: true })));
  }
  // Everything else (photos, live updates, scripts) is left to the browser as normal.
});
