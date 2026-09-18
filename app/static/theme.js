/* Переключатель темы: светлая → тёмная → как в системе.
   Выбор хранится в localStorage и применяется к <html data-theme>.
   Начальное значение ставит инлайновый скрипт в <head>, чтобы не мигало. */
(function () {
  "use strict";

  var KEY = "sport-theme";
  // Порядок обхода; первый — значение по умолчанию.
  var ORDER = ["light", "dark", "auto"];
  var t = window.portalStrings || function (key, fallback) { return fallback; };
  var LABELS = {
    auto: t("theme.auto", "Тема: как в системе"),
    light: t("theme.light", "Тема: светлая"),
    dark: t("theme.dark", "Тема: тёмная"),
  };

  function read() {
    try {
      var value = localStorage.getItem(KEY);
      return ORDER.indexOf(value) >= 0 ? value : "light";
    } catch (e) {
      return "light";
    }
  }

  function apply(value) {
    var root = document.documentElement;
    // Атрибут ставим всегда, включая "auto": без него CSS даёт светлую тему.
    root.setAttribute("data-theme", value);

    var buttons = document.querySelectorAll("[data-theme-toggle]");
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].setAttribute("data-theme-state", value);
      buttons[i].title = LABELS[value];
      buttons[i].setAttribute("aria-label", LABELS[value]);
    }
  }

  function cycle() {
    var next = ORDER[(ORDER.indexOf(read()) + 1) % ORDER.length];
    try {
      localStorage.setItem(KEY, next);
    } catch (e) {
      /* приватный режим — тема продержится до перезагрузки */
    }
    apply(next);
  }

  apply(read());
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-theme-toggle]");
    if (button) {
      event.preventDefault();
      cycle();
    }
  });
})();
