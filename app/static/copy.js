/* Кнопка «Скопировать» у всего, что человек переносит в другое место:
   адрес и ключ MCP, настройки клиентов, код подтверждения аккаунта.
   Помечается в шаблоне атрибутом data-copy — кнопку дорисовывает скрипт,
   так что без JavaScript остаётся обычное выделение текста. */
(function () {
  "use strict";

  var t = window.portalStrings || function (key, fallback) { return fallback; };

  function fallbackCopy(node) {
    // Старый способ — для страницы по http без безопасного контекста,
    // где navigator.clipboard недоступен (портал в аудитории по IP).
    var range = document.createRange();
    range.selectNodeContents(node);
    var selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    try {
      return document.execCommand("copy");
    } catch (e) {
      return false;
    }
  }

  function copy(node, button) {
    var text = node.textContent;
    var done = function (ok) {
      button.textContent = ok ? t("copy.done", "Скопировано") : t("copy.failed", "Выдели и скопируй");
      button.classList.toggle("copied", ok);
      setTimeout(function () {
        button.textContent = t("copy.button", "Скопировать");
        button.classList.remove("copied");
      }, 1800);
    };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(
        function () { done(true); },
        function () { done(fallbackCopy(node)); }
      );
    } else {
      done(fallbackCopy(node));
    }
  }

  var nodes = document.querySelectorAll("[data-copy]");
  for (var i = 0; i < nodes.length; i++) {
    (function (node) {
      var wrap = document.createElement("div");
      wrap.className = "copyable";
      node.parentNode.insertBefore(wrap, node);
      wrap.appendChild(node);
      var button = document.createElement("button");
      button.type = "button";
      button.className = "copy-btn";
      button.textContent = t("copy.button", "Скопировать");
      button.addEventListener("click", function () { copy(node, button); });
      wrap.appendChild(button);
    })(nodes[i]);
  }
})();
