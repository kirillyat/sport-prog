/* Обратный отсчёт до старта соревнования.
   Элемент: <span data-countdown data-start="ISO" data-end="ISO">.
   Время приходит в UTC с сервером — Date.parse разберёт его с учётом зоны. */
(function () {
  "use strict";

  function pad(value) {
    return String(value).padStart(2, "0");
  }

  var t = window.portalStrings || function (key, fallback) { return fallback; };
  var plural = window.portalPlural || function (count, forms) { return String(forms).split("|")[0]; };

  function setState(el, state, text) {
    el.textContent = text;
    if (el.dataset.state !== state) el.dataset.state = state;
  }

  function render(el) {
    var start = el.dataset.start ? Date.parse(el.dataset.start) : null;
    var end = el.dataset.end ? Date.parse(el.dataset.end) : null;
    var now = Date.now();
    var target, prefix, state;

    if (start && now < start) {
      target = start; prefix = t("countdown.before_start", "до старта"); state = "upcoming";
    } else if (end && now < end) {
      target = end; prefix = t("countdown.before_end", "до конца"); state = "live";
    } else if (start && !end && now - start < 86400000) {
      setState(el, "live", t("countdown.live", "идёт сейчас"));
      return;
    } else {
      setState(el, "past", t("countdown.past", "завершено"));
      return;
    }

    var left = Math.max(0, Math.floor((target - now) / 1000));
    var days = Math.floor(left / 86400);
    left -= days * 86400;
    var hours = Math.floor(left / 3600);
    var minutes = Math.floor((left % 3600) / 60);
    var seconds = left % 60;

    var daysPart = days ? days + " " + plural(days, t("countdown.days", "день|дня|дней")) + " " : "";
    setState(el, state, prefix + ": " + daysPart + pad(hours) + ":" + pad(minutes) + ":" + pad(seconds));
  }

  function tick() {
    var nodes = document.querySelectorAll("[data-countdown]");
    for (var i = 0; i < nodes.length; i++) render(nodes[i]);
  }

  tick();
  setInterval(tick, 1000);
})();
