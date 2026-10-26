// Service Worker for DeckStream PWA
// Provides background push notifications for game install/readiness and offline shell caching.

const CACHE_NAME = 'deckstream-shell-v5';
const SHELL_URLS = [
  '/',
  '/index.html',
  '/manifest.webmanifest',
  '/fonts/barlow-latin-400-normal.woff2',
  '/fonts/barlow-latin-500-normal.woff2',
  '/fonts/barlow-latin-600-normal.woff2',
  '/fonts/barlow-condensed-latin-600-normal.woff2',
  '/fonts/barlow-condensed-latin-700-normal.woff2'
];

self.addEventListener('install', (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(SHELL_URLS).catch(() => {});
    })
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    Promise.all([
      self.clients.claim(),
      caches.keys().then((keys) => {
        return Promise.all(
          keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))
        );
      })
    ])
  );
});

// Fetch event: network first, cache fallback for navigation
self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;
  const url = new URL(event.request.url);
  // Only this site's own files. Other origins (game covers from Steam and SteamGridDB) go straight to the network: a
  // service-worker fetch() of them falls under the site's CSP connect-src and fails, which broke every cover image.
  if (url.origin !== self.location.origin) return;

  // Do not cache API requests or external CDN ranges
  if (url.pathname.startsWith('/api/')) {
    return;
  }

  event.respondWith(
    fetch(event.request).catch(() => {
      return caches.match(event.request);
    })
  );
});

// Push notification event: handle background lock-screen notifications
self.addEventListener('push', (event) => {
  let data = {};
  if (event.data) {
    try {
      data = event.data.json();
    } catch (e) {
      data = { body: event.data.text() };
    }
  }

  const title = data.title || '🎮 DeckStream';
  const options = {
    body: data.body || 'Your Steam Deck has an update.',
    icon: data.icon || 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><circle cx="50" cy="50" r="50" fill="%231a9fff"/></svg>',
    badge: data.badge || 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><circle cx="50" cy="50" r="50" fill="%231a9fff"/></svg>',
    data: data.data || { url: '/' },
    vibrate: [200, 100, 200],
    tag: data.tag || 'deckstream-status',
    renotify: true
  };

  event.waitUntil(
    self.registration.showNotification(title, options)
  );
});

// Notification click event: focus or open the app window
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const rawUrl = (event.notification.data && event.notification.data.url) || '/';
  const targetUrl = (typeof rawUrl === 'string' && rawUrl.startsWith('/') && !rawUrl.startsWith('//')) ? rawUrl : '/';

  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
      for (const client of clientList) {
        if (client.url && 'focus' in client) {
          return client.focus();
        }
      }
      if (self.clients.openWindow) {
        return self.clients.openWindow(targetUrl);
      }
    })
  );
});
