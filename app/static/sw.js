// Минимальный service worker: нужен браузерам, чтобы сайт можно было установить на главный экран.
// Ничего не кэширует — данные растений всегда свежие и не остаются в кэше устройства.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
