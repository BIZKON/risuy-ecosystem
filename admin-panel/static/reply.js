// Вложение файла и запись голосового в форме ручного ответа лиду.
//
// Форма #reply (multipart) ушла бы обычным submit и без этого скрипта — здесь
// только клиентский UX: показать имя/размер выбранного файла и записать голос
// с микрофона. CSP запрещает inline-JS (script-src 'self'), поэтому всё поведение
// вынесено сюда, а в шаблоне у элементов стабильные id/data-*.
//
// Запись голоса: MediaRecorder → Blob → кладём как File в СКРЫТЫЙ файл-инпут
// #rec-file (name="voice") через DataTransfer, чтобы он ушёл тем же обычным
// submit в тот же multipart-эндпоинт /leads/{id}/reply с тем же CSRF-токеном.
// Бэкенд распознаёт #rec-file как голос и ставит kind='voice' (бот конвертит в
// ogg/opus). mimeType пробуем в порядке предпочтения; Safari отдаст audio/mp4.
(function () {
  "use strict";

  // ── 1. Показ имени/размера выбранного файла-вложения ──────────────────────
  var fileInput = document.getElementById("reply-file");
  var fileName = document.getElementById("reply-file-name");
  if (fileInput && fileName) {
    fileInput.addEventListener("change", function () {
      var f = fileInput.files && fileInput.files[0];
      if (!f) {
        fileName.textContent = fileName.getAttribute("data-empty") || "Файл не выбран";
        return;
      }
      var mb = (f.size / 1024 / 1024).toFixed(2);
      fileName.textContent = f.name + " · " + mb + " МБ";
    });
  }

  // ── 2. Запись голосового через MediaRecorder ──────────────────────────────
  var rec = document.getElementById("rec");
  var toggle = document.getElementById("rec-toggle");
  var status = document.getElementById("rec-status");
  var preview = document.getElementById("rec-preview");
  var recFile = document.getElementById("rec-file");

  // Нет нужных элементов или браузер без MediaRecorder/getUserMedia — прячем UI.
  var hasSupport =
    typeof window.MediaRecorder !== "undefined" &&
    navigator.mediaDevices &&
    typeof navigator.mediaDevices.getUserMedia === "function";

  if (!rec || !toggle || !status || !preview || !recFile) return;
  if (!hasSupport) {
    rec.hidden = true;
    return;
  }

  // Лимит длительности (сек) — авто-стоп. По умолчанию 120.
  var maxSec = parseInt(rec.getAttribute("data-max-sec"), 10);
  if (!(maxSec > 0)) maxSec = 120;

  var labelIdle = toggle.getAttribute("data-label-idle") || "Записать голос";
  var labelRec = toggle.getAttribute("data-label-rec") || "Остановить";

  var recorder = null;
  var stream = null;
  var chunks = [];
  var tickTimer = null;
  var stopTimer = null;
  var startedAt = 0;

  // Подбор поддерживаемого mimeType. Safari не умеет isTypeSupported для opus —
  // тогда отдаём дефолт (Safari запишет audio/mp4).
  function pickMime() {
    var candidates = ["audio/ogg;codecs=opus", "audio/webm;codecs=opus"];
    if (typeof MediaRecorder.isTypeSupported === "function") {
      for (var i = 0; i < candidates.length; i++) {
        if (MediaRecorder.isTypeSupported(candidates[i])) return candidates[i];
      }
    }
    return ""; // дефолт браузера
  }

  // Расширение файла по mime (бэкенд/бот ориентируется на mime, имя — для UX).
  function extForMime(mime) {
    if (mime.indexOf("ogg") !== -1) return "ogg";
    if (mime.indexOf("webm") !== -1) return "webm";
    if (mime.indexOf("mp4") !== -1 || mime.indexOf("mpeg") !== -1) return "m4a";
    return "bin";
  }

  function fmtTime(totalSec) {
    var m = Math.floor(totalSec / 60);
    var s = totalSec % 60;
    return m + ":" + (s < 10 ? "0" : "") + s;
  }

  function clearTimers() {
    if (tickTimer) { clearInterval(tickTimer); tickTimer = null; }
    if (stopTimer) { clearTimeout(stopTimer); stopTimer = null; }
  }

  function releaseStream() {
    if (stream) {
      stream.getTracks().forEach(function (t) { t.stop(); });
      stream = null;
    }
  }

  function setIdleLabel() {
    toggle.textContent = labelIdle;
    rec.classList.remove("is-recording");
  }

  // Положить записанный blob в скрытый файл-инпут через DataTransfer — чтобы он
  // ушёл обычным submit. DataTransfer.files можно присвоить input.files.
  function attachBlob(blob, mime) {
    var ext = extForMime(mime);
    var file = new File([blob], "voice." + ext, { type: mime || "audio/ogg" });
    try {
      var dt = new DataTransfer();
      dt.items.add(file);
      recFile.files = dt.files;
    } catch (e) {
      // Очень старый браузер без присваивания input.files — деградируем: прячем
      // превью и сообщаем, что запись не поддержана (текст/файл всё равно работают).
      status.textContent = "Запись не поддержана этим браузером.";
      preview.hidden = true;
      return;
    }
    var url = URL.createObjectURL(blob);
    preview.src = url;
    preview.hidden = false;
  }

  function startRecording() {
    status.textContent = "Запрашиваю доступ к микрофону…";
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (s) {
      stream = s;
      chunks = [];
      var mime = pickMime();
      try {
        recorder = mime ? new MediaRecorder(s, { mimeType: mime }) : new MediaRecorder(s);
      } catch (e) {
        recorder = new MediaRecorder(s); // фолбэк на дефолт, если mimeType отвергнут
      }

      recorder.addEventListener("dataavailable", function (ev) {
        if (ev.data && ev.data.size > 0) chunks.push(ev.data);
      });

      recorder.addEventListener("stop", function () {
        clearTimers();
        releaseStream();
        setIdleLabel();
        var type = recorder.mimeType || mime || "audio/ogg";
        var blob = new Blob(chunks, { type: type });
        chunks = [];
        if (blob.size === 0) {
          status.textContent = "Пустая запись — попробуйте ещё раз.";
          return;
        }
        attachBlob(blob, type);
        status.textContent = "Голос записан. Уйдёт при отправке.";
      });

      recorder.start();
      startedAt = Date.now();
      toggle.textContent = labelRec;
      rec.classList.add("is-recording");

      // Таймер длительности + авто-стоп по лимиту.
      tickTimer = setInterval(function () {
        var sec = Math.floor((Date.now() - startedAt) / 1000);
        status.textContent = "Запись… " + fmtTime(sec) + " / " + fmtTime(maxSec);
      }, 250);
      status.textContent = "Запись… 0:00 / " + fmtTime(maxSec);
      stopTimer = setTimeout(stopRecording, maxSec * 1000);
    }).catch(function (err) {
      // Отказ микрофона / нет устройства — показываем сообщение, не падаем.
      releaseStream();
      if (err && err.name === "NotAllowedError") {
        status.textContent = "Доступ к микрофону запрещён.";
      } else if (err && err.name === "NotFoundError") {
        status.textContent = "Микрофон не найден.";
      } else {
        status.textContent = "Не удалось начать запись.";
      }
      setIdleLabel();
    });
  }

  function stopRecording() {
    clearTimers();
    if (recorder && recorder.state !== "inactive") {
      recorder.stop(); // дальше — обработчик 'stop' выше
    } else {
      releaseStream();
      setIdleLabel();
    }
  }

  toggle.addEventListener("click", function () {
    if (recorder && recorder.state === "recording") {
      stopRecording();
    } else {
      // Новая запись затирает прошлую: чистим скрытый инпут и превью.
      try { recFile.value = ""; } catch (e) { /* ignore */ }
      preview.hidden = true;
      preview.removeAttribute("src");
      startRecording();
    }
  });

  // На уходе со страницы освобождаем микрофон, если запись ещё идёт.
  window.addEventListener("beforeunload", function () {
    clearTimers();
    releaseStream();
  });
})();
