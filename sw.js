/* Service Worker:保守策略,不快取 Gradio 的 API / 上傳 / 串流,避免問答資料錯亂 */
const VERSION = "v1";
const STATIC_CACHE = `rag-static-${VERSION}`;
const PRECACHE = [
  "/offline.html",
  "/manifest.json",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
  "/icons/apple-touch-icon.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE).then((c) => c.addAll(PRECACHE)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((k) => k !== STATIC_CACHE).map((k) => caches.delete(k)))
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // 頁面導覽:網路優先,離線時顯示離線頁
  if (req.mode === "navigate") {
    event.respondWith(fetch(req).catch(() => caches.match("/offline.html")));
    return;
  }

  // 圖示 / manifest:快取優先
  if (url.pathname.startsWith("/icons/") || url.pathname === "/manifest.json") {
    event.respondWith(caches.match(req).then((hit) => hit || fetch(req)));
  }
  // 其餘(Gradio 資源與 API)一律交給瀏覽器處理
});
