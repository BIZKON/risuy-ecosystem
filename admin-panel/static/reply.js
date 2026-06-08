// Композер ответа лиду: НЕСКОЛЬКО вложений (файлы + голос) + превью списком до отправки.
//
// CSP запрещает inline-JS (script-src 'self') — всё поведение здесь. Вложения копятся в
// JS-массиве `selected` (File[]) и зеркалятся в #attachments (name="files", multiple)
// через DataTransfer; уходят обычным multipart-submit с тем же CSRF-токеном. Голос
// пишется MediaRecorder'ом и ДОБАВЛЯЕТСЯ в тот же список (взаимоисключения с файлом нет).
// Превью — список чипов в #composer-preview: миниатюра картинки / иконка документа с
// «Открыть» / <audio controls> голоса (blob: разрешён в CSP media-src/img-src), у каждого
// «✕ убрать». Бэкенд читает files[], классифицирует каждый по MIME (audio/* → voice) и
// кладёт по строке outbox на каждое вложение (Telegram шлёт их отдельными сообщениями).
(function () {
  "use strict";

  var composer = document.getElementById("composer");
  var form = composer && composer.closest("form");
  if (!composer || !form) return;

  var attachBtn   = document.getElementById("attach-btn");
  var picker      = document.getElementById("file-picker");   // транзитный (без name) — для выбора
  var attachInput = document.getElementById("attachments");   // name="files" — реальный сабмитный
  var recToggle   = document.getElementById("rec-toggle");
  var status      = document.getElementById("rec-status");
  var preview     = document.getElementById("composer-preview");
  var textarea    = document.getElementById("reply-text");

  var MAX_FILES = 10;     // UX-потолок (авторитетный кэп — на бэкенде)
  var selected = [];      // File[]
  var chipURLs = [];      // blob: URL текущих чипов (revoke при rebuild)

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;   // textContent: имена файлов без HTML-инъекций
    return n;
  }
  function fmtSize(b) { return (b / 1024 / 1024).toFixed(2) + " МБ"; }

  // Зеркалим selected → реальный сабмитный инпут files[] через DataTransfer.
  function syncInput() {
    try {
      var dt = new DataTransfer();
      selected.forEach(function (f) { dt.items.add(f); });
      if (attachInput) attachInput.files = dt.files;
    } catch (e) { /* очень старый браузер без присваивания input.files — без мультивложений */ }
  }
  function clearURLs() {
    chipURLs.forEach(function (u) { try { URL.revokeObjectURL(u); } catch (e) {} });
    chipURLs = [];
  }

  function chipFor(file, index) {
    var url = URL.createObjectURL(file); chipURLs.push(url);
    var isImg = (file.type || "").indexOf("image/") === 0;
    var isAudio = (file.type || "").indexOf("audio/") === 0;
    var row = el("div", isAudio ? "attach-audio" : "attach-chip");

    if (isAudio) {
      var audio = el("audio"); audio.controls = true; audio.src = url; audio.preload = "metadata";
      row.appendChild(audio);
      row.appendChild(el("span", "attach-chip__meta", "Голос · " + fmtSize(file.size)));
    } else {
      var thumb = el("span", "attach-chip__thumb");
      if (isImg) { var img = el("img"); img.src = url; img.alt = ""; thumb.appendChild(img); }
      else thumb.textContent = "📄";
      row.appendChild(thumb);
      var body = el("div", "attach-chip__body");
      body.appendChild(el("span", "attach-chip__name", file.name));
      body.appendChild(el("span", "attach-chip__meta", fmtSize(file.size)));
      row.appendChild(body);
      var open = el("button", "chip-btn", "Открыть");
      open.type = "button"; open.title = "Открыть в новой вкладке";
      open.addEventListener("click", function () { window.open(url, "_blank", "noopener"); });
      row.appendChild(open);
    }
    var rm = el("button", "chip-btn chip-btn--danger", "✕");
    rm.type = "button"; rm.title = "Убрать"; rm.setAttribute("aria-label", "Убрать вложение");
    rm.addEventListener("click", function () { selected.splice(index, 1); rebuild(); });
    row.appendChild(rm);
    return row;
  }

  function rebuild() {
    syncInput();
    if (!preview) return;
    clearURLs();
    preview.textContent = "";
    if (!selected.length) { preview.hidden = true; return; }
    selected.forEach(function (f, i) { preview.appendChild(chipFor(f, i)); });
    preview.hidden = false;
  }

  function addFiles(list) {
    for (var i = 0; i < list.length; i++) {
      if (selected.length >= MAX_FILES) {
        if (status) status.textContent = "Можно не больше " + MAX_FILES + " вложений в одном ответе.";
        break;
      }
      selected.push(list[i]);
    }
    rebuild();
  }

  // ── Скрепка → выбрать файлы (можно несколько раз, докидывая в список) ──────
  if (attachBtn && picker) {
    attachBtn.addEventListener("click", function () { picker.click(); });
    picker.addEventListener("change", function () {
      if (picker.files && picker.files.length) addFiles(picker.files);
      try { picker.value = ""; } catch (e) {}   // повторный выбор того же файла снова триггерит change
    });
  }

  // ── Авто-рост поля ввода ──────────────────────────────────────────────────
  if (textarea) {
    var grow = function () {
      textarea.style.height = "auto";
      textarea.style.height = Math.min(textarea.scrollHeight, 168) + "px";
    };
    textarea.addEventListener("input", grow);
  }

  // ── Запись голоса → ДОБАВЛЯЕТСЯ в список вложений ──────────────────────────
  var hasSupport = typeof window.MediaRecorder !== "undefined" &&
    navigator.mediaDevices && typeof navigator.mediaDevices.getUserMedia === "function";
  if (!recToggle || !hasSupport) { if (recToggle && !hasSupport) recToggle.hidden = true; return; }

  var maxSec = parseInt(composer.getAttribute("data-max-sec"), 10);
  if (!(maxSec > 0)) maxSec = 120;
  var recorder = null, stream = null, chunks = [], tickTimer = null, stopTimer = null, startedAt = 0;

  function pickMime() {
    var c = ["audio/ogg;codecs=opus", "audio/webm;codecs=opus"];
    if (typeof MediaRecorder.isTypeSupported === "function")
      for (var i = 0; i < c.length; i++) if (MediaRecorder.isTypeSupported(c[i])) return c[i];
    return "";
  }
  function extForMime(m) {
    if (m.indexOf("ogg") > -1) return "ogg";
    if (m.indexOf("webm") > -1) return "webm";
    if (m.indexOf("mp4") > -1 || m.indexOf("mpeg") > -1) return "m4a";
    return "bin";
  }
  function fmt(t) { var m = Math.floor(t / 60), s = t % 60; return m + ":" + (s < 10 ? "0" : "") + s; }
  function clearTimers() {
    if (tickTimer) { clearInterval(tickTimer); tickTimer = null; }
    if (stopTimer) { clearTimeout(stopTimer); stopTimer = null; }
  }
  function release() { if (stream) { stream.getTracks().forEach(function (t) { t.stop(); }); stream = null; } }
  function setRecUI(on) {
    composer.classList.toggle("is-recording", on);
    recToggle.title = on ? "Остановить запись (идёт запись)" : "Записать голос";
  }

  function start() {
    if (selected.length >= MAX_FILES) { status.textContent = "Можно не больше " + MAX_FILES + " вложений."; return; }
    status.textContent = "Запрашиваю доступ к микрофону…";
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (s) {
      stream = s; chunks = [];
      var mime = pickMime();
      try { recorder = mime ? new MediaRecorder(s, { mimeType: mime }) : new MediaRecorder(s); }
      catch (e) { recorder = new MediaRecorder(s); }

      recorder.addEventListener("dataavailable", function (ev) {
        if (ev.data && ev.data.size > 0) chunks.push(ev.data);
      });
      recorder.addEventListener("stop", function () {
        clearTimers(); release(); setRecUI(false);
        var type = recorder.mimeType || mime || "audio/ogg";
        var blob = new Blob(chunks, { type: type }); chunks = [];
        if (blob.size === 0) { status.textContent = "Пустая запись — попробуйте ещё раз."; return; }
        selected.push(new File([blob], "voice." + extForMime(type), { type: type }));
        rebuild();
        status.textContent = "Голос добавлен — можно переслушать. Уйдёт при «Отправить».";
      });

      recorder.start(); startedAt = Date.now(); setRecUI(true);
      tickTimer = setInterval(function () {
        status.textContent = "Запись… " + fmt(Math.floor((Date.now() - startedAt) / 1000)) + " / " + fmt(maxSec);
      }, 250);
      status.textContent = "Запись… 0:00 / " + fmt(maxSec);
      stopTimer = setTimeout(stop, maxSec * 1000);
    }).catch(function (err) {
      release(); setRecUI(false);
      status.textContent =
        err && err.name === "NotAllowedError" ? "Доступ к микрофону запрещён." :
        err && err.name === "NotFoundError" ? "Микрофон не найден." :
        "Не удалось начать запись.";
    });
  }
  function stop() {
    clearTimers();
    if (recorder && recorder.state !== "inactive") recorder.stop();
    else { release(); setRecUI(false); }
  }

  recToggle.addEventListener("click", function () {
    if (recorder && recorder.state === "recording") stop();
    else start();
  });
  window.addEventListener("beforeunload", function () { clearTimers(); release(); });
})();
