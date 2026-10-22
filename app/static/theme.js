/* Оформление: выбор одной из цветовых тем.
   Выбор хранится в localStorage и применяется к <html data-theme>.
   Начальное значение ставит инлайновый скрипт в <head>, чтобы не мигало. */
(function () {
  "use strict";

  var KEY = "sport-theme";
  // Порядок неважен — важно, что список тот же, что в CSS и в меню.
  var THEMES = ["amber", "parchment", "mandarin", "lime", "finland", "japan", "coal", "sweden", "brazil", "oxford", "neon", "ice", "magenta", "matrix", "halloween", "newyear", "valentine", "cosmos"];
  // Праздничные темы (app/services/seasons.py): действуют только в свой
  // праздник, который сервер отмечает у <html data-season>.
  var SEASONAL = ["halloween", "newyear", "valentine", "cosmos"];
  var season = document.documentElement.getAttribute("data-season") || "";
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
      // Праздник прошёл — праздничный выбор больше не действует.
      if (SEASONAL.indexOf(value) >= 0 && value !== season) value = null;
      // Без выбора — праздничная тема, если праздник идёт, иначе та же,
      // что у голого :root в CSS.
      return THEMES.indexOf(value) >= 0 ? value : (season || "finland");
    } catch (e) {
      return season || "finland";
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

    // Значок праздника — тем, у кого сейчас не праздничная тема.
    var invites = document.querySelectorAll("[data-season-btn]");
    for (var j = 0; j < invites.length; j++) {
      invites[j].hidden = !season || value === season;
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
