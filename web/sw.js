// uEye service worker. Deliberately does no caching (live video + recordings
// must always hit the network); it exists so the app is installable and so
// push notifications can be added later.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
