/* Service Worker — push-уведомления */
self.addEventListener('install', e => { self.skipWaiting(); });
self.addEventListener('activate', e => { e.waitUntil(clients.claim()); });

self.addEventListener('push', e => {
  if (!e.data) return;
  let d;
  try { d = e.data.json(); } catch { d = { title: 'Messenger', body: e.data.text() }; }

  // Если страница открыта и в фокусе — не показываем push (JS сам обработает)
  e.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
      const focused = list.some(c => c.focused && c.visibilityState === 'visible');
      if (focused) return; // окно активно — не дублируем уведомление

      const opts = {
        body: d.body || '',
        icon: '/static/icon192.png',
        badge: '/static/icon192.png',
        tag: d.tag || 'msg',
        data: { url: '/', room_id: d.room_id },
        renotify: true,
        vibrate: [200, 100, 200],
        silent: false,
      };
      return self.registration.showNotification(d.title || 'Messenger', opts);
    })
  );
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  e.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
      for (const c of list) {
        if (c.url.includes(self.location.origin)) {
          c.focus();
          if (e.notification.data && e.notification.data.room_id) {
            c.postMessage({ type: 'open_room', room_id: e.notification.data.room_id });
          }
          return;
        }
      }
      return clients.openWindow('/');
    })
  );
});
