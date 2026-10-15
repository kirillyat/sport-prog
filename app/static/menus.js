/* Выпадающие меню на <details class="popup">: «Ещё», аватарка, тема, язык.
   Открываются силами браузера, а скрипт только закрывает их — щелчком мимо
   и клавишей Esc. Без него открытое меню висело бы, пока по нему же не
   щёлкнут ещё раз, и два меню могли быть открыты разом. */
(function () {
  "use strict";

  function openPopups() {
    return document.querySelectorAll("details.popup[open]");
  }

  document.addEventListener("click", function (event) {
    var popups = openPopups();
    for (var i = 0; i < popups.length; i++) {
      // Меню темы лежит внутри меню аватарки — его открытие не должно
      // закрывать родителя.
      if (!popups[i].contains(event.target)) popups[i].open = false;
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    var popups = openPopups();
    for (var i = 0; i < popups.length; i++) popups[i].open = false;
  });
})();
