// Atlas service worker: caches the app shell only. Financial data is never cached.
const SHELL = "atlas-shell-v1";
const FILES = ["/", "/static/app.css", "/static/app.js", "/static/icon.svg", "/manifest.webmanifest"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(FILES)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  const isShell = e.request.method === "GET" && url.origin === location.origin &&
    (url.pathname === "/" || url.pathname.startsWith("/static/") || url.pathname === "/manifest.webmanifest");
  if (!isShell) return; // API calls go straight to the network
  e.respondWith(fetch(e.request).then((resp) => {
    const copy = resp.clone();
    caches.open(SHELL).then((c) => c.put(e.request, copy));
    return resp;
  }).catch(() => caches.match(e.request)));
});
