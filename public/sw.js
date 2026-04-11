// Minimal service worker — makes woltspace installable as a PWA.
// No offline caching for now; everything requires a live connection anyway.

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
