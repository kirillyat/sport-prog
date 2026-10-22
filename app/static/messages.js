/* Новые сообщения преподавателя без перезагрузки: на странице задания и на
   главной. Раз в 20 секунд спрашиваем сервер, нет ли новее последнего, —
   и только пока вкладку видно: спрятанная вкладка сервер не дёргает.
   Разметку присылает сервер той же, что на странице, поэтому скрипт ничего
   не собирает сам. Без JavaScript лента просто обновляется с перезагрузкой. */
(function () {
  "use strict";

  var EVERY = 20000;
  var boxes = document.querySelectorAll("[data-messages-src]");
  if (!boxes.length || !window.fetch) return;

  function poll(box) {
    if (document.hidden) return;
    var url = box.getAttribute("data-messages-src")
      + "?after=" + encodeURIComponent(box.getAttribute("data-after") || "0");
    fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (data) {
        if (!data || !data.items || !data.items.length) return;
        var list = box.querySelector("[data-message-list]");
        // Сервер отдаёт свежие первыми; вставляем с конца, чтобы самое новое
        // оказалось сверху.
        for (var i = data.items.length - 1; i >= 0; i--) {
          var item = data.items[i];
          if (list.querySelector('[data-id="' + item.id + '"]')) continue;
          var holder = document.createElement("div");
          holder.innerHTML = item.html;
          var node = holder.firstElementChild;
          if (!node) continue;
          node.classList.add("arrived");
          list.insertBefore(node, list.firstChild);
        }
        box.setAttribute("data-after", String(data.items[0].id));
        box.hidden = false;
      })
      .catch(function () {
        /* сеть моргнула — спросим в следующий раз */
      });
  }

  Array.prototype.forEach.call(boxes, function (box) {
    setInterval(function () { poll(box); }, EVERY);
  });
  // Вернулись на вкладку — сразу проверить, а не ждать до 20 секунд.
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) Array.prototype.forEach.call(boxes, poll);
  });
})();
