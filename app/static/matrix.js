/* «Матрица»: столбцы бегущих символов на фоне, пока выбрана эта тема.
   Холст лежит под содержимым (z-index: -1 в style.css): виден в полях и
   просветах между карточками, текст и кнопки не перекрывает. Тему меняют
   без перезагрузки — поэтому следим за data-theme у <html> и сами
   заводим или убираем холст. */
(function () {
  "use strict";

  var root = document.documentElement;
  // Полуширинная катакана, как в фильме, и то, из чего состоит код.
  // Катакана есть в стандартных шрифтах и Windows, и macOS; холст сам
  // берёт её из запасного шрифта, если в основном её нет.
  var GLYPHS = ("ｦｱｳｴｵｶｷｹｺｻｼｽｾｿﾀﾂﾃﾅﾆﾇﾈﾊﾋﾎﾏﾐﾑﾒﾓﾔﾕﾗﾘﾜ0123456789{}[]<>=+-*/;:_$#").split("");
  var SIZE = 18;            // кегль и шаг сетки, в CSS-пикселях
  // 20 кадров в секунду: лёгкие рывки — часть образа, а процессор не греем.
  var FRAME = 1000 / 20;
  var still = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;

  var canvas = null, ctx = null, timer = 0, last = 0;
  var width = 0, height = 0, rows = 0, columns = [];
  var font = "monospace", head = "#d8ffe0", tail = "#00ff41";

  function glyph() {
    return GLYPHS[Math.floor(Math.random() * GLYPHS.length)];
  }

  function launch(column, anywhere) {
    // Первые столбцы разбросаны по высоте экрана, иначе при включении
    // все падали бы разом сверху, как занавес.
    column.y = anywhere ? Math.random() * rows : -Math.random() * rows * 0.6;
    column.speed = 0.25 + Math.random() * 0.6;
    column.length = 8 + Math.floor(Math.random() * 18);
  }

  function fill(column) {
    while (column.cells.length < rows) column.cells.push(glyph());
  }

  function layout() {
    var ratio = Math.min(window.devicePixelRatio || 1, 2);
    var widthChanged = window.innerWidth !== width;
    width = window.innerWidth;
    height = window.innerHeight;
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.font = SIZE + "px " + font;
    ctx.textBaseline = "top";
    rows = Math.ceil(height / SIZE) + 1;
    // На телефоне высота окна меняется при прокрутке (прячется адресная
    // строка) — столбцы тогда не пересоздаём, чтобы дождь не дёргался.
    if (widthChanged) {
      columns = [];
      for (var i = 0, count = Math.ceil(width / SIZE); i < count; i++) {
        var column = { cells: [] };
        launch(column, true);
        columns.push(column);
      }
    }
    for (var j = 0; j < columns.length; j++) fill(columns[j]);
  }

  function draw() {
    ctx.clearRect(0, 0, width, height);
    for (var i = 0; i < columns.length; i++) {
      var column = columns[i];
      var top = Math.floor(column.y);
      for (var k = 0; k < column.length; k++) {
        var row = top - k;
        if (row < 0 || row >= rows) continue;
        // Голова светлая, хвост гаснет к концу.
        ctx.globalAlpha = k === 0 ? 1 : (1 - k / column.length) * 0.85;
        ctx.fillStyle = k === 0 ? head : tail;
        ctx.fillText(column.cells[row], i * SIZE, row * SIZE);
      }
      // Символы в хвосте изредка меняются — столбец живой, а не лента.
      if (Math.random() < 0.3) column.cells[Math.floor(Math.random() * rows)] = glyph();
      column.y += column.speed;
      if (column.y - column.length > rows) launch(column, false);
    }
    ctx.globalAlpha = 1;
  }

  function tick(now) {
    timer = window.requestAnimationFrame(tick);
    if (now - last < FRAME) return;
    last = now;
    draw();
  }

  function moving() {
    return !(still && still.matches);
  }

  function start() {
    if (canvas) return;
    canvas = document.createElement("canvas");
    canvas.className = "matrix-rain";
    canvas.setAttribute("aria-hidden", "true");
    ctx = canvas.getContext && canvas.getContext("2d");
    if (!ctx) {
      canvas = null;
      return;
    }
    var style = window.getComputedStyle(root);
    tail = style.getPropertyValue("--ink").trim() || tail;
    font = (style.getPropertyValue("--mono").trim() || "monospace") + ", monospace";
    width = 0;
    document.body.insertBefore(canvas, document.body.firstChild);
    layout();
    // Кто просил меньше движения, получает один неподвижный кадр — узор
    // остаётся, мельтешения нет.
    if (moving()) {
      timer = window.requestAnimationFrame(tick);
    } else {
      draw();
    }
  }

  function stop() {
    if (!canvas) return;
    window.cancelAnimationFrame(timer);
    canvas.remove();
    canvas = null;
    ctx = null;
    columns = [];
  }

  function sync() {
    if (root.getAttribute("data-theme") === "matrix") {
      start();
    } else {
      stop();
    }
  }

  window.addEventListener("resize", function () {
    if (!canvas) return;
    layout();
    if (!moving()) draw();
  });
  if (still && still.addEventListener) {
    still.addEventListener("change", function () {
      stop();
      sync();
    });
  }
  if (window.MutationObserver) {
    new MutationObserver(sync).observe(root, { attributes: true, attributeFilter: ["data-theme"] });
  }
  sync();
})();
