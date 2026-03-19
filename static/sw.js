/* Service Worker — push-уведомления */
self.addEventListener('install', e => { self.skipWaiting(); });
self.addEventListener('activate', e => { e.waitUntil(clients.claim()); });

// Храним данные активного входящего звонка
let _pendingCallData = null;

self.addEventListener('push', e => {
  if (!e.data) return;
  let d;
  try { d = e.data.json(); } catch { d = { title: 'Messenger', body: e.data.text() }; }

  const isCall = d.tag === 'call';

  e.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
      // Если страница в фокусе — не показываем push для сообщений, но для звонка — всегда
      const focused = list.some(c => c.focused && c.visibilityState === 'visible');
      if (focused && !isCall) return;

      // Для звонка сохраняем данные чтобы страница могла их получить
      if (isCall && d.call_data) {
        _pendingCallData = d.call_data;
        // Отправляем данные звонка всем открытым вкладкам
        list.forEach(c => c.postMessage({ type: 'incoming_call', call_data: d.call_data }));
      }

      const opts = {
        body: d.body || '',
        icon: '/static/icon192.png',
        badge: '/static/icon192.png',
        tag: d.tag || 'msg',
        data: { url: '/', room_id: d.room_id, call_data: d.call_data || null },
        renotify: true,
        vibrate: isCall ? [500, 200, 500, 200, 500] : [200, 100, 200],
        silent: false,
        requireInteraction: isCall, // звонок остаётся пока не ответят
        // Кнопки только для звонка
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
      // Если нажали "Сбросить"
      if (action === 'decline') {
        list.forEach(c => c.postMessage({ type: 'decline_call', call_data: data.call_data }));
        _pendingCallData = null;
        return;
      }

      // Открываем или фокусируем вкладку
      const openClient = list.find(c => c.url.includes(self.location.origin));
      if (openClient) {
        openClient.focus();
        // Передаём данные звонка на страницу
        if (data.call_data) {
          openClient.postMessage({ type: 'incoming_call', call_data: data.call_data, auto_accept: action === 'accept' });
        } else if (data.room_id) {
          openClient.postMessage({ type: 'open_room', room_id: data.room_id });
        }
        _pendingCallData = null;
        return;
      }
      // Страница закрыта — открываем и передаём данные через URL
      const url = data.call_data
        ? `/?pending_call=${encodeURIComponent(JSON.stringify(data.call_data))}`
        : '/';
      return clients.openWindow(url);
    })
  );
});

// Когда страница запрашивает pending call
self.addEventListener('message', e => {
  if (e.data && e.data.type === 'get_pending_call') {
    e.source.postMessage({ type: 'pending_call', call_data: _pendingCallData });
    _pendingCallData = null;
  }
});
