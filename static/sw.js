/* Service Worker — push-уведомления */
self.addEventListener('install', e => { self.skipWaiting(); });
self.addEventListener('activate', e => { e.waitUntil(clients.claim()); });

let _pendingCallData = null;

self.addEventListener('push', e => {
  if (!e.data) return;
  let d;
  try { d = e.data.json(); } catch { d = { title: 'Messenger', body: e.data.text() }; }

  const isCall = d.tag === 'call';

  e.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
      const focused = list.some(c => c.focused);

      if (isCall && d.call_data) {
        _pendingCallData = d.call_data;
        // Отправляем на все открытые вкладки — там заиграет рингтон
        list.forEach(c => c.postMessage({
          type: 'incoming_call',
          call_data: d.call_data,
          auto_accept: false
        }));
      }

      // Для сообщений не показываем если страница в фокусе
      if (focused && !isCall) return;

      const callType = d.call_data ? d.call_data.call_type : 'voice';
      const opts = {
        body: d.body || '',
        icon: '/static/icon192.png',
        badge: '/static/icon192.png',
        tag: d.tag || 'msg',
        data: { url: '/', room_id: d.room_id, call_data: d.call_data || null },
        renotify: true,
        vibrate: isCall ? [500, 200, 500, 200, 500] : [200, 100, 200],
        silent: false,
        sound: 'default',
        requireInteraction: isCall,
        ...(isCall ? {
          actions: [
            { action: 'accept', title: '✅ Взять' },
            { action: 'decline', title: '❌ Сбросить' }
          ]
        } : {})
      };
      return self.registration.showNotification(d.title || 'Messenger', opts);
    })
  );
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  const data = e.notification.data || {};
  const action = e.action;

  e.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
      if (action === 'decline') {
        list.forEach(c => c.postMessage({ type: 'decline_call', call_data: data.call_data }));
        _pendingCallData = null;
        return;
      }

      const openClient = list.find(c => c.url.includes(self.location.origin));
      if (openClient) {
        openClient.focus();
        if (data.call_data) {
          openClient.postMessage({
            type: 'incoming_call',
            call_data: data.call_data,
            auto_accept: action === 'accept'
          });
        } else if (data.room_id) {
          openClient.postMessage({ type: 'open_room', room_id: data.room_id });
        }
        _pendingCallData = null;
        return;
      }

      // Страница закрыта — открываем с параметром
      const url = data.call_data
        ? `/?pending_call=${encodeURIComponent(JSON.stringify(data.call_data))}&auto_accept=${action === 'accept' ? '1' : '0'}`
        : '/';
      _pendingCallData = null;
      return clients.openWindow(url);
    })
  );
});

self.addEventListener('message', e => {
  if (!e.data) return;
  if (e.data.type === 'get_pending_call') {
    e.source.postMessage({ type: 'pending_call', call_data: _pendingCallData });
    _pendingCallData = null;
  }
});
