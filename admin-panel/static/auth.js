/* Парадная «ИИ-Агент Про»: переключение вкладок Вход ↔ Регистрация без перезагрузки.
   Прогрессивное улучшение: ссылки [data-auth-go] имеют рабочий href="/login?tab=…"
   (полная перезагрузка без JS) — здесь лишь перехватываем клик для мгновенного toggle. */
(function () {
  "use strict";
  var card = document.querySelector(".auth-card");
  if (!card) return;

  function setTab(tab) {
    if (tab !== "signin" && tab !== "signup") return;
    card.setAttribute("data-tab", tab);
    // Фокус на первое поле активной формы (удобство; не критично).
    var sel = tab === "signup" ? ".auth-pane--signup" : ".auth-pane--signin";
    var first = card.querySelector(sel + " input:not([type=hidden])");
    if (first) {
      try { first.focus(); } catch (e) { /* noop */ }
    }
  }

  card.addEventListener("click", function (ev) {
    var el = ev.target.closest ? ev.target.closest("[data-auth-go]") : null;
    if (!el) return;
    ev.preventDefault();
    setTab(el.getAttribute("data-auth-go"));
  });
})();
