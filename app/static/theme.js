/* Оформление: выбор одной из цветовых тем.
   Выбор хранится в localStorage и применяется к <html data-theme>.
   Начальное значение ставит инлайновый скрипт в <head>, чтобы не мигало. */
(function () {
  "use strict";

  var KEY = "sport-theme";
  // Порядок неважен — важно, что список тот же, что в CSS и в меню.
  var THEMES = ["amber", "parchment", "mandarin", "lime", "finland", "japan", "coal", "sweden", "brazil", "oxford", "neon", "ice", "magenta"];
  // Старые значения переключателя «светлая/тёмная»: у людей они уже
  // сохранены, и терять их выбор из-за переименования незачем.
  var LEGACY = { light: "amber", dark: "coal", poster: "amber", paper: "parchment", night: "coal", chalk: "brazil", indigo: "oxford", frost: "finland", mint: "amber", terracotta: "mandarin", lilac: "parchment", pine: "brazil", ultramarine: "oxford", plum: "magenta", coffee: "coal" };

  function read() {
    try {
      var value = localStorage.getItem(KEY);
      value = LEGACY[value] || value;
      // «Как в системе» больше нет: у кого она была, один раз получает тему
      // по светлоте своей системы, дальше выбор его.
      if (value === "auto") {
        value = window.matchMedia
          && window.matchMedia("(prefers-color-scheme: dark)").matches ? "coal" : "amber";
      }
      return THEMES.indexOf(value) >= 0 ? value : "amber";
    } catch (e) {
      return "amber";
    }
  }

  function apply(value) {
    document.documentElement.setAttribute("data-theme", value);

    var options = document.querySelectorAll("[data-theme-set]");
    for (var i = 0; i < options.length; i++) {
      var current = options[i].getAttribute("data-theme-set") === value;
      options[i].classList.toggle("current", current);
      if (current) {
        options[i].setAttribute("aria-current", "true");
      } else {
        options[i].removeAttribute("aria-current");
      }
    }
  }

  function choose(value) {
    try {
      localStorage.setItem(KEY, value);
    } catch (e) {
      /* приватный режим — тема продержится до перезагрузки */
    }
    apply(value);
  }

  apply(read());
  document.addEventListener("click", function (event) {
    var option = event.target.closest("[data-theme-set]");
    if (!option) return;
    event.preventDefault();
    choose(option.getAttribute("data-theme-set"));
    // Закрываем меню: выбор сделан, держать его раскрытым незачем.
    var menu = option.closest("details");
    if (menu) menu.open = false;
  });
})();
