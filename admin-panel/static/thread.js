// Точечное обновление ленты переписки лида БЕЗ перезагрузки страницы.
//
// Прежде карточка лида обновлялась через <meta http-equiv="refresh"> — полная
// перезагрузка раз в N секунд СТИРАЛА текст, который оператор набирал в форме
// ответа. Теперь обновляем только #thread-box (саму ленту сообщений); форма
// ввода лежит ВНЕ этого контейнера и потому не сбрасывается.
//
// Источник: GET /leads/{id}/thread → партиал _thread.html (server-rendered).
// CSP: script-src 'self' (этот файл) + connect-src 'self' (fetch) — разрешено.
// Если сессия истекла, fetch редиректит на /login (r.redirected) — прекращаем.
(function () {
  "use strict";

  var box = document.getElementById("thread-box");
  if (!box) return;

  var url = box.getAttribute("data-thread-url");
  var sec = parseInt(box.getAttribute("data-refresh"), 10);
  if (!url || !(sec > 0)) return;

  var timer = null;
  function stop() {
    if (timer) { clearInterval(timer); timer = null; }
  }

  function tick() {
    if (document.hidden) return; // вкладка скрыта — не дёргаем сервер
    fetch(url, { credentials: "same-origin", cache: "no-store" })
      .then(function (r) {
        // r.redirected → ушли на /login (сессия истекла); !r.ok → ошибка. Стоп.
        if (r.redirected || !r.ok) { stop(); return null; }
        return r.text();
      })
      .then(function (html) {
        if (html === null) return;
        // Заменяем ТОЛЬКО ленту. Форма ответа вне #thread-box — не затрагивается.
        box.innerHTML = html;
      })
      .catch(function () {
        // транзиентная сетевая ошибка — просто повторим на следующем тике
      });
  }

  timer = setInterval(tick, sec * 1000);
})();
