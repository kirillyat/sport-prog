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
    extraKeys: {
      Tab: function (cm) {
        // Tab внутри редактора — отступ, а не уход фокуса.
        if (cm.somethingSelected()) cm.indentSelection("add");
        else cm.replaceSelection("    ", "end");
      },
      "Shift-Tab": function (cm) { cm.indentSelection("subtract"); },
      // Привычное «отправить»: как в большинстве судей.
      "Ctrl-Enter": function () { submitForm("run"); },
      "Cmd-Enter": function () { submitForm("run"); }
    }
  });

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

  var timer = null;
  editor.on("change", function () {
    clearTimeout(timer);
    timer = setTimeout(function () {
      try { localStorage.setItem(key, editor.getValue()); } catch (e) {}
    }, 800);
  });
})();
