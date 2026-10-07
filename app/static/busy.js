/* Кнопка, за которой долгий запрос, показывает, что работа идёт.
   Форма: <form data-busy="busy.check"> — значение есть ключ строки из js_strings.
   У кнопки может быть свой data-busy: в форме задачи «прогон» и «сдача» —
   разные действия, и подпись должна сказать, какое из них идёт.
   Без отклика кнопку жмут ещё раз или обновляют страницу, а обновление
   посреди POST уводит на адрес действия вместо раздела. */
(function () {
  "use strict";

  var t = window.portalStrings || function (key, fallback) { return fallback; };

  function busy(form, button) {
    if (!button || button.disabled) return;
    button.dataset.idle = button.innerHTML;
    button.textContent = t(button.dataset.busy || form.dataset.busy, "…");
    button.setAttribute("aria-busy", "true");
    // Отключаем после отправки: отключённая кнопка до отправки не попала бы
    // в данные формы, а у некоторых браузеров и вовсе отменила бы её.
    // Соседние кнопки — тоже: второй запрос, пока идёт первый, судья
    // всё равно не примет.
    var others = form.querySelectorAll("button");
    setTimeout(function () {
      for (var i = 0; i < others.length; i++) {
        if (!others[i].disabled) {
          others[i].disabled = true;
          others[i].dataset.wasEnabled = "1";
        }
      }
    }, 0);
  }

  function restore() {
    var buttons = document.querySelectorAll("form[data-busy] button[aria-busy]");
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].innerHTML = buttons[i].dataset.idle;
      buttons[i].removeAttribute("aria-busy");
    }
    var locked = document.querySelectorAll("form[data-busy] button[data-was-enabled]");
    for (var j = 0; j < locked.length; j++) {
      locked[j].disabled = false;
      locked[j].removeAttribute("data-was-enabled");
    }
  }

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form.matches || !form.matches("form[data-busy]")) return;
    busy(form, event.submitter || form.querySelector("button[type=submit], button:not([type])"));
  });

  // «Назад» достаёт страницу из кеша вместе с застывшей кнопкой.
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) restore();
  });
})();
