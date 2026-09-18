/* Строки для скриптов. Инлайнового кода на страницах не держим, поэтому
   переводы приезжают данными — в <script type="application/json">, —
   а этот файл разбирает их и раздаёт остальным скриптам.
   Грузится первым: у defer порядок тегов сохраняется. */
(function () {
  "use strict";

  var data = {};
  try {
    var node = document.getElementById("js-strings");
    if (node) data = JSON.parse(node.textContent) || {};
  } catch (e) {
    /* без словаря скрипты покажут свои запасные строки */
  }

  window.portalStrings = function (key, fallback) {
    return data[key] || fallback || key;
  };

  /* Формы слова приходят одной строкой: «день|дня|дней», «day|days».
     Какую выбрать — решает язык страницы, а не число само по себе. */
  window.portalPlural = function (count, forms) {
    var parts = String(forms).split("|");
    var lang = document.documentElement.lang || "ru";
    if (parts.length >= 3) {
      var tail = Math.abs(count) % 100;
      if (tail >= 11 && tail <= 14) return parts[2];
      tail %= 10;
      if (tail === 1) return parts[0];
      if (tail >= 2 && tail <= 4) return parts[1];
      return parts[2];
    }
    if (parts.length < 2) return parts[0];
    // Во французском ноль — тоже единственное число, в английском уже нет.
    var single = lang === "fr" ? Math.abs(count) < 2 : Math.abs(count) === 1;
    return single ? parts[0] : parts[1];
  };
})();
