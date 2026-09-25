/* Редактор кода на странице задачи.
   CodeMirror лежит в app/static/vendor — без CDN, чтобы портал работал
   в аудитории без интернета и не зависел от чужого хоста.
   Инициализация здесь, а не инлайном на странице: правило портала —
   никакого JavaScript в шаблонах. */
(function () {
  "use strict";

  var area = document.getElementById("code");
  if (!area || typeof CodeMirror === "undefined") return;

  var editor = CodeMirror.fromTextArea(area, {
    mode: "python",
    lineNumbers: true,
    indentUnit: 4,
    tabSize: 4,
    // Пробелы вместо табов: питон не прощает смешения.
    indentWithTabs: false,
    autoCloseBrackets: true,
    matchBrackets: true,
    styleActiveLine: true,
    lineWrapping: true,
    // Высота по содержимому: без этого CodeMirror рисует только видимый
    // кусок и при height:auto оставляет пустоту.
    viewportMargin: Infinity,
    extraKeys: {
      Tab: function (cm) {
        // Tab внутри редактора — отступ, а не уход фокуса.
        if (cm.somethingSelected()) cm.indentSelection("add");
        else cm.replaceSelection("    ", "end");
      },
      "Shift-Tab": function (cm) { cm.indentSelection("subtract"); },
      // Привычное «отправить»: как в большинстве судей.
      "Ctrl-Enter": function () { submitForm("run"); },
      "Cmd-Enter": function () { submitForm("run"); },
      "Ctrl-/": toggleComment,
      "Cmd-/": toggleComment,
      "Alt-Up": function (cm) { moveLine(cm, -1); },
      "Alt-Down": function (cm) { moveLine(cm, 1); },
      // Рефлекс «сохранить» не должен открывать диалог браузера: черновик
      // и так сохраняется сам, но нажать Ctrl+S спокойнее, чем поверить.
      "Ctrl-S": saveDraftNow,
      "Cmd-S": saveDraftNow
    }
  });

  /* Комментарий строкой. Своя реализация вместо ещё одного файла
     CodeMirror: правило одно — «# » в начале, с учётом отступа. */
  function toggleComment(cm) {
    var from = cm.getCursor("from").line;
    var to = cm.getCursor("to").line;
    var lines = [];
    var commented = true;
    for (var n = from; n <= to; n++) {
      var text = cm.getLine(n);
      lines.push(text);
      if (text.trim() && text.replace(/^\s*/, "").indexOf("#") !== 0) commented = false;
    }
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];
      if (!line.trim()) continue;
      var indent = line.match(/^\s*/)[0];
      var body = line.slice(indent.length);
      lines[i] = commented
        ? indent + body.replace(/^#\s?/, "")
        : indent + "# " + body;
    }
    cm.replaceRange(
      lines.join("\n"),
      { line: from, ch: 0 },
      { line: to, ch: cm.getLine(to).length }
    );
  }

  /* Перенос строки вверх или вниз — привычный Alt+стрелка. */
  function moveLine(cm, step) {
    var cursor = cm.getCursor();
    var target = cursor.line + step;
    if (target < 0 || target >= cm.lineCount()) return;
    var here = cm.getLine(cursor.line);
    var there = cm.getLine(target);
    cm.replaceRange(there, { line: cursor.line, ch: 0 }, { line: cursor.line, ch: here.length });
    cm.replaceRange(here, { line: target, ch: 0 }, { line: target, ch: there.length });
    cm.setCursor({ line: target, ch: cursor.ch });
  }

  // Формы отправляют содержимое textarea — синхронизируем перед отправкой.
  function submitForm(name) {
    var form = document.querySelector('form[data-editor-form="' + name + '"]');
    if (form) { editor.save(); form.requestSubmit ? form.requestSubmit() : form.submit(); }
  }

  var forms = document.querySelectorAll("form[data-editor-form]");
  for (var i = 0; i < forms.length; i++) {
    forms[i].addEventListener("submit", function () { editor.save(); });
  }

  // Черновик переживает случайное закрытие вкладки. Ключ — адрес задачи,
  // поэтому у разных задач черновики не путаются.
  var key = "sport-draft:" + location.pathname;
  try {
    var saved = localStorage.getItem(key);
    if (saved && !area.value.trim()) editor.setValue(saved);
  } catch (e) { /* приватный режим — просто без черновика */ }

  var state = document.querySelector("[data-draft-state]");
  var saved_label = state ? state.getAttribute("data-draft-state") : "";

  function saveDraftNow() {
    try {
      localStorage.setItem(key, editor.getValue());
      if (state) state.textContent = saved_label;
    } catch (e) { /* приватный режим — просто без черновика */ }
  }

  var timer = null;
  editor.on("change", function () {
    clearTimeout(timer);
    timer = setTimeout(saveDraftNow, 800);
  });
})();
