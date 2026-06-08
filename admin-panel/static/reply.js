// Композер ответа лиду: вложение файла + запись голоса + ПРЕВЬЮ до отправки.
//
// CSP запрещает inline-JS (script-src 'self') — всё поведение здесь, у элементов
// шаблона стабильные id. Скрытые инпуты: #reply-file (name="file", выбранный файл)
// и #rec-file (name="voice", запись MediaRecorder через DataTransfer). Бэкенд
// распознаёт voice как голос (kind='voice'); один и тот же multipart-submit, тот же
// CSRF-токен. Превью (миниатюра картинки / иконка документа с «Открыть» / плеер
// голоса) рисуем в #composer-preview — blob: URL разрешён в CSP media-src/img-src,
// поэтому запись можно ПЕРЕСЛУШАТЬ, а документ ОТКРЫТЬ ещё до отправки.
//
// Взаимоисключение: ответ несёт ОДНО вложение — выбор файла стирает запись и наоборот.
(function () {
  "use strict";

  var composer = document.getElementById("composer");
  var form = composer && composer.closest("form");
  if (!composer || !form) return;

  var attachBtn = document.getElementById("attach-btn");
  var fileInput = document.getElementById("reply-file");  // name="file"
  var recFile   = document.getElementById("rec-file");    // name="voice"
  var recToggle = document.getElementById("rec-toggle");
  var status    = document.getElementById("rec-status");
  var preview   = document.getElementById("composer-preview");
  var textarea  = document.getElementById("reply-text");

  // ── Управление blob: URL (освобождаем прошлый, чтобы не течь) ──────────────
  var currentURL = null;
  function setURL(u) {
    if (currentURL) { try { URL.revokeObjectURL(currentURL); } catch (e) {} }
    currentURL = u || null;
    return currentURL;
  }
  function clearPreview() {
    if (preview) { preview.textContent = ""; preview.hidden = true; }
    setURL(null);
  }
  function resetAttachment(which) {            // 'all' | 'file' | 'voice'
    if (which !== "voice" && fileInput) { try { fileInput.value = ""; } catch (e) {} }
    if (which !== "file"  && recFile)   { try { recFile.value = ""; } catch (e) {} }
    clearPreview();
    if (status) status.textContent = "";
  }

  // ── Хелперы DOM (textContent для имён файлов — без HTML-инъекций) ──────────
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function fmtSize(b) { return (b / 1024 / 1024).toFixed(2) + " МБ"; }
  function removeBtn(onClick) {
    var b = el("button", "chip-btn chip-btn--danger", "✕");
    b.type = "button"; b.title = "Убрать вложение";
    b.setAttribute("aria-label", "Убрать вложение");
    b.addEventListener("click", onClick);
    return b;
  }

  // ── Превью выбранного файла: миниатюра картинки / иконка документа + «Открыть» ──
  function showFilePreview(file) {
    if (!preview) return;
    clearPreview();
    var url = setURL(URL.createObjectURL(file));
    var isImg = (file.type || "").indexOf("image/") === 0;

    var chip = el("div", "attach-chip");
    var thumb = el("span", "attach-chip__thumb");
    if (isImg) { var img = el("img"); img.src = url; img.alt = ""; thumb.appendChild(img); }
    else { thumb.textContent = "📄"; }  // 📄
    chip.appendChild(thumb);

    var body = el("div", "attach-chip__body");
    body.appendChild(el("span", "attach-chip__name", file.name));
    body.appendChild(el("span", "attach-chip__meta", fmtSize(file.size)));
    chip.appendChild(body);

    var actions = el("div", "attach-chip__actions");
    var open = el("button", "chip-btn", "Открыть");
    open.type = "button"; open.title = "Открыть в новой вкладке";
    open.addEventListener("click", function () { window.open(url, "_blank", "noopener"); });
    actions.appendChild(open);
    actions.appendChild(removeBtn(function () { resetAttachment("file"); }));
    chip.appendChild(actions);

    preview.appendChild(chip);
    preview.hidden = false;
  }

  // ── Превью записанного голоса: плеер (переслушать) + длительность + убрать ──
  function showVoicePreview(blob, seconds) {
    if (!preview) return;
    clearPreview();
    var url = setURL(URL.createObjectURL(blob));
    var box = el("div", "attach-audio");
    var audio = el("audio");
    audio.controls = true; audio.src = url; audio.preload = "metadata";
    box.appendChild(audio);
    box.appendChild(el("span", "attach-chip__meta", "Голос" + (seconds ? " · " + seconds + " c" : "")));
    box.appendChild(removeBtn(function () { resetAttachment("voice"); }));
    preview.appendChild(box);
    preview.hidden = false;
  }

  // ── Вложение файла (скрепка открывает скрытый file-input) ─────────────────
  if (attachBtn && fileInput) {
    attachBtn.addEventListener("click", function () { fileInput.click(); });
    fileInput.addEventListener("change", function () {
      var f = fileInput.files && fileInput.files[0];
      if (!f) return;
      if (recFile) { try { recFile.value = ""; } catch (e) {} }  // взаимоисключение с голосом
      showFilePreview(f);
    });
  }

  // ── Авто-рост поля ввода (как в мессенджерах) ─────────────────────────────
  if (textarea) {
    var grow = function () {
      textarea.style.height = "auto";
      textarea.style.height = Math.min(textarea.scrollHeight, 140) + "px";
    };
    textarea.addEventListener("input", grow);
  }

  // ── Запись голоса (MediaRecorder) ─────────────────────────────────────────
  var hasSupport =
    typeof window.MediaRecorder !== "undefined" &&
    navigator.mediaDevices &&
    typeof navigator.mediaDevices.getUserMedia === "function";

  if (!recToggle || !recFile || !hasSupport) {
    if (recToggle && !hasSupport) recToggle.hidden = true;  // нет записи в браузере — прячем микрофон
    return;                                                  // текст и файл всё равно работают
  }

  var maxSec = parseInt(composer.getAttribute("data-max-sec"), 10);
  if (!(maxSec > 0)) maxSec = 120;

  var recorder = null, stream = null, chunks = [], tickTimer = null, stopTimer = null, startedAt = 0;

  function pickMime() {
    var c = ["audio/ogg;codecs=opus", "audio/webm;codecs=opus"];
    if (typeof MediaRecorder.isTypeSupported === "function")
      for (var i = 0; i < c.length; i++) if (MediaRecorder.isTypeSupported(c[i])) return c[i];
    return "";  // дефолт браузера (Safari → audio/mp4)
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

  function attachBlob(blob, mime, seconds) {
    var file = new File([blob], "voice." + extForMime(mime), { type: mime || "audio/ogg" });
    try {
      var dt = new DataTransfer();
      dt.items.add(file);
      recFile.files = dt.files;
    } catch (e) {
      status.textContent = "Запись не поддержана этим браузером.";
      return;
    }
    if (fileInput) { try { fileInput.value = ""; } catch (e) {} }  // взаимоисключение с файлом
    showVoicePreview(blob, seconds);
  }

  function start() {
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
        var secs = Math.max(1, Math.round((Date.now() - startedAt) / 1000));
        if (blob.size === 0) { status.textContent = "Пустая запись — попробуйте ещё раз."; return; }
        attachBlob(blob, type, secs);
        status.textContent = "Голос записан — можно переслушать. Уйдёт при «Отправить».";
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
    else { resetAttachment("all"); start(); }  // новая запись затирает прошлое вложение
  });
  window.addEventListener("beforeunload", function () { clearTimers(); release(); });
})();
