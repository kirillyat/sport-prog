/* Редактор кода на странице задачи.
   CodeMirror лежит в app/static/vendor — без CDN, чтобы портал работал
   в аудитории без интернета и не зависел от чужого хоста.
   Инициализация здесь, а не инлайном на странице: правило портала —
   никакого JavaScript в шаблонах. */
(function () {
  "use strict";

  var area = document.getElementById("code");
  if (!area || typeof CodeMirror === "undefined") return;
  var t = window.portalStrings || function (key, fallback) { return fallback; };

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
      "Cmd-S": saveDraftNow,
      "Ctrl-Space": function (cm) { cm.showHint({ hint: suggest, completeSingle: false }); }
    }
  });

  /* CodeMirror меряет колонку номеров строк один раз — при создании. Если
     в этот момент редактор ещё не отрисован (ссылка открыта в фоновой
     вкладке, страница поднята из кеша, браузер отложил вёрстку), ширина
     выходит нулевой, и номера ложатся прямо поверх кода: «a,1 b», «pr2int».
     Поэтому перемеряем, как только у редактора появилась или сменилась
     ширина, а заодно когда доехали шрифты и догрузилась страница. */
  var wrapper = editor.getWrapperElement();
  var measuredWidth = wrapper.offsetWidth;
  function remeasure() { editor.refresh(); }
  if (window.ResizeObserver) {
    // Только по ширине: высота редактора растёт с кодом, и перемер на каждую
    // новую строку был бы лишней работой.
    new ResizeObserver(function () {
      var width = wrapper.offsetWidth;
      if (width !== measuredWidth) {
        measuredWidth = width;
        remeasure();
      }
    }).observe(wrapper);
  }
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(remeasure);
  window.addEventListener("load", remeasure);
  window.addEventListener("pageshow", function (event) { if (event.persisted) remeasure(); });

  /* Развернуть редактор поверх условия. Выбор запоминается в браузере: после
     «Прогнать» страница приходит заново, и студенту, который пишет в большом
     редакторе, не нужно разворачивать его каждый раз. */
  var expandButton = document.querySelector("[data-editor-expand]");
  var EXPANDED_KEY = "sport-editor-expanded";

  function setExpanded(on) {
    document.body.classList.toggle("editor-expanded", on);
    if (expandButton) expandButton.setAttribute("aria-pressed", on ? "true" : "false");
    editor.refresh();
  }

  if (expandButton) {
    expandButton.hidden = false;
    expandButton.addEventListener("click", function () {
      var on = !document.body.classList.contains("editor-expanded");
      setExpanded(on);
      try { localStorage.setItem(EXPANDED_KEY, on ? "1" : ""); } catch (e) { /* приватный режим */ }
      editor.focus();
    });
    var remembered = false;
    try { remembered = localStorage.getItem(EXPANDED_KEY) === "1"; } catch (e) { /* приватный режим */ }
    if (remembered) {
      setExpanded(true);
      // Браузер прокрутил страницу к #result до того, как колонка стала слоем
      // поверх, — внутри слоя результат нужно показать заново.
      var result = location.hash === "#result" && document.getElementById("result");
      if (result) result.scrollIntoView();
    }
    // Esc сворачивает, но не тот Esc, которым закрывают подсказку редактора.
    // Слушаем на погружении — до CodeMirror: он сам помечает любой Esc
    // обработанным, и по метке подсказку от свёртки уже не отличить.
    document.addEventListener("keydown", function (event) {
      if (event.key !== "Escape") return;
      if (!document.body.classList.contains("editor-expanded")) return;
      if (document.querySelector(".CodeMirror-hints")) return;
      setExpanded(false);
      try { localStorage.setItem(EXPANDED_KEY, ""); } catch (e) { /* приватный режим */ }
    }, true);
  }

  /* Автодополнение. Свой источник вместо готового «по любым словам»: тот
     предлагал бы и слова из комментариев, и опечатки, набранные выше.
     Здесь — ключевые слова и встроенные функции питона плюс имена, которые
     студент уже завёл в этом же решении. */
  var PYTHON = (
    "and as assert async await break class continue def del elif else except " +
    "False finally for from global if import in is lambda None nonlocal not or " +
    "pass raise return True try while with yield " +
    "abs all any bin bool bytes chr dict divmod enumerate filter float format " +
    "frozenset getattr hasattr hash input int isinstance iter len list map max " +
    "min next object open ord pow print range repr reversed round set setattr " +
    "slice sorted str sum tuple type zip"
  ).split(" ");

  function suggest(cm) {
    var cursor = cm.getCursor();
    var line = cm.getLine(cursor.line);
    var start = cursor.ch;
    while (start && /[\w_]/.test(line.charAt(start - 1))) start--;
    var word = line.slice(start, cursor.ch);
    if (!word) return null;

    // В строке и в комментарии подсказывать нечего.
    var token = cm.getTokenTypeAt(cursor);
    if (token === "string" || token === "comment") return null;

    var seen = {};
    var words = [];
    var text = cm.getValue().match(/[A-Za-z_][\w_]*/g) || [];
    for (var i = 0; i < text.length; i++) {
      if (text[i] !== word) seen[text[i]] = true;
    }
    for (var j = 0; j < PYTHON.length; j++) seen[PYTHON[j]] = true;
    for (var name in seen) {
      if (name.lastIndexOf(word, 0) === 0 && name !== word) words.push(name);
    }
    if (!words.length) return null;
    words.sort();
    return {
      list: words,
      from: CodeMirror.Pos(cursor.line, start),
      to: CodeMirror.Pos(cursor.line, cursor.ch)
    };
  }

  // Подсказка всплывает сама со второй буквы слова — но ничего не вставляет
  // молча: выбор всегда за человеком.
  editor.on("inputRead", function (cm, change) {
    if (change.origin !== "+input" || !/[\w_]/.test(change.text[0])) return;
    var cursor = cm.getCursor();
    var before = cm.getLine(cursor.line).slice(0, cursor.ch);
    if (!/[\w_]{2,}$/.test(before)) return;
    cm.showHint({ hint: suggest, completeSingle: false });
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

  /* Textarea лежит внутри формы, и кнопки отправляют её саму — поэтому
     перед отправкой редактор переписывает в неё свой текст. Ctrl+Enter
     жмёт ту же кнопку, что и мышь: куда отправлять, решает кнопка. */
  var form = area.form;
  function submitForm(action) {
    var button = form && form.querySelector('button[data-action="' + action + '"]');
    if (!button || button.disabled) return;
    editor.save();
    if (form.requestSubmit) form.requestSubmit(button);
    else button.click();
  }
  if (form) form.addEventListener("submit", function () { editor.save(); saveDraftNow(); });

  /* Черновик переживает закрытую вкладку и уход со страницы. Ключ — адрес
     задачи, поэтому у разных задач черновики не путаются. Берём его со
     страницы, а не из адресной строки: после кнопки там /run или /submit,
     и правки после прогона копились бы под чужим ключом и терялись.

     Страница приносит свой код — последнюю сдачу или только что
     отправленное — и время этого кода. Побеждает более свежий: несданные
     правки после последней сдачи не должны пропадать при возвращении
     на задачу, а сдача с другого компьютера — затираться старым черновиком. */
  var key = "sport-draft:" + (area.getAttribute("data-draft-key") || location.pathname);
  var pageAt = Number(area.getAttribute("data-code-at")) || 0;
  var state = document.querySelector("[data-draft-state]");
  var saved_label = state ? state.getAttribute("data-draft-state") : "";

  function readDraft() {
    var raw = localStorage.getItem(key);
    if (!raw) return null;
    try {
      var parsed = JSON.parse(raw);
      if (parsed && typeof parsed.code === "string") return parsed;
    } catch (e) { /* черновик старого формата — просто текст */ }
    return { code: raw, at: 0 };
  }

  try {
    var draft = readDraft();
    var current = area.value;
    if (draft && draft.code.trim() && draft.code !== current &&
        (!current.trim() || draft.at > pageAt)) {
      editor.setValue(draft.code);
      if (state) state.textContent = t("draft.restored", "восстановлен несданный черновик");
    }
  } catch (e) { /* приватный режим — просто без черновика */ }

  function saveDraftNow() {
    try {
      localStorage.setItem(key, JSON.stringify({ code: editor.getValue(), at: Date.now() }));
      if (state) state.textContent = saved_label;
    } catch (e) { /* приватный режим — просто без черновика */ }
  }

  var timer = null;
  editor.on("change", function () {
    clearTimeout(timer);
    timer = setTimeout(saveDraftNow, 800);
  });
})();
