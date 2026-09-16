"""Показ ноутбука на портале.

Рендерим сами: markdown, формулы, подсвеченный код и картинки. HTML-вывод
ячеек не показываем вообще — в ноутбуке он произвольный, а страница
открывается на нашем домене под сессией студента. Кому нужен полный вид
с интерактивом, скачивает файл.

Всё считается на сервере: ни одной внешней библиотеки в браузер не уходит.
Формулы превращаются в MathML, который браузеры рисуют сами, код красит
Pygments. Ни JavaScript, ни дополнительных шрифтов это не требует.
"""

from __future__ import annotations

import base64
import binascii
import html
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

from latex2mathml.converter import convert as latex_to_mathml
from markdown_it import MarkdownIt
from mdit_py_plugins.dollarmath import dollarmath_plugin
from pygments import highlight as pygments_highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound

# Разметка MathML, которую пропускаем наружу. Список закрытый: latex2mathml
# отдаёт содержимое \text{...} как есть, и злой ноутбук протащил бы туда
# настоящий тег. Всё, чего здесь нет, выбрасывается.
MATHML_TAGS = frozenset("""
math mrow mi mn mo ms mtext mspace msub msup msubsup munder mover munderover
mfrac msqrt mroot mstyle mpadded mphantom mtable mtr mtd mmultiscripts
mprescripts none merror
""".split())

# Оформительские атрибуты и только они: ни href, ни style, ни обработчиков.
MATHML_ATTRS = frozenset("""
display displaystyle mathvariant mathsize dir stretchy fence separator accent
accentunder largeop movablelimits symmetric maxsize minsize linethickness
columnalign rowalign columnspacing rowspacing open close separators
depth height width lspace rspace voffset scriptlevel xmlns
""".split())

# Подсветку дальше этого не делаем: страница всё равно нечитаемая, а Pygments
# на мегабайтном листинге заметно думает.
MAX_HIGHLIGHT_BYTES = 200_000

_formatter = HtmlFormatter(nowrap=True)


class _MathMLCleaner(HTMLParser):
    """Оставляет от разметки только разрешённые теги MathML, текст экранирует."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []
        self._open: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag not in MATHML_TAGS:
            return
        kept = "".join(
            f' {name}="{html.escape(value or "", quote=True)}"'
            for name, value in attrs
            if name in MATHML_ATTRS
        )
        self.parts.append(f"<{tag}{kept}>")
        self._open.append(tag)

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag in MATHML_TAGS:
            self.parts.append(f"<{tag}/>")

    def handle_endtag(self, tag: str) -> None:
        if tag in MATHML_TAGS and tag in self._open:
            self.parts.append(f"</{tag}>")
            self._open.remove(tag)

    def handle_data(self, data: str) -> None:
        self.parts.append(html.escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.parts.append(f"&#{name};")

    def result(self) -> str:
        return "".join(self.parts)


def _mathml(tex: str, display: bool) -> str:
    """Формула в MathML. Не разобралась — показываем исходник, а не пустоту."""
    try:
        raw = latex_to_mathml(tex, display="block" if display else "inline")
    except Exception:
        return f'<code class="math-raw">{html.escape(tex)}</code>'
    cleaner = _MathMLCleaner()
    cleaner.feed(raw)
    cleaner.close()
    return cleaner.result()


def _render_math_inline(self, tokens, idx, options, env):  # noqa: ARG001
    return _mathml(tokens[idx].content, display=False)


def _render_math_block(self, tokens, idx, options, env):  # noqa: ARG001
    return f'<div class="math-block">{_mathml(tokens[idx].content, display=True)}</div>'


# html=False: сырой HTML внутри markdown экранируется, а не выполняется.
_md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
_md.use(dollarmath_plugin, double_inline=True)
_md.add_render_rule("math_inline", _render_math_inline)
_md.add_render_rule("math_block", _render_math_block)
_md.add_render_rule("math_inline_double", _render_math_inline)
_md.add_render_rule("math_block_label", _render_math_block)


def highlight(source: str, language: str) -> str:
    """Подсветка кода. Язык неизвестен или файл огромный — отдаём как есть."""
    if len(source) > MAX_HIGHLIGHT_BYTES:
        return html.escape(source)
    try:
        lexer = get_lexer_by_name(language or "text")
    except ClassNotFound:
        return html.escape(source)
    return pygments_highlight(source, lexer, _formatter)

# Ноутбук больше этого не показываем: страница всё равно будет нечитаемой.
MAX_PREVIEW_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_CHARS = 20_000
SAFE_IMAGES = ("image/png", "image/jpeg", "image/gif", "image/webp")
# SVG отдаём только внутри <img>: там он не исполняет скрипты и не ходит
# наружу за ресурсами, в отличие от вставки в разметку страницы.
SVG_MIME = "image/svg+xml"
ANSI = re.compile(r"\x1b\[[0-9;]*m")

PREVIEWABLE = {".ipynb", ".md", ".py", ".txt", ".csv", ".json", ".tex"}


@dataclass(slots=True)
class Output:
    kind: str                 # text | image | math | skipped
    text: str = ""
    src: str = ""


@dataclass(slots=True)
class Cell:
    kind: str                 # markdown | code | raw
    html: str = ""
    source: str = ""
    language: str = ""
    outputs: list[Output] = field(default_factory=list)


def is_previewable(filename: str, size: int) -> bool:
    from pathlib import Path

    return Path(filename).suffix.lower() in PREVIEWABLE and size <= MAX_PREVIEW_BYTES


def _text(value: Any) -> str:
    """В ipynb строка бывает и списком строк — формат допускает оба вида."""
    if isinstance(value, list):
        return "".join(str(part) for part in value)
    return "" if value is None else str(value)


def render_markdown(source: str) -> str:
    return _md.render(source)


def _output(raw: dict) -> Output | None:
    kind = raw.get("output_type")
    if kind == "stream":
        return Output("text", _text(raw.get("text"))[:MAX_OUTPUT_CHARS])
    if kind == "error":
        trace = ANSI.sub("", _text(raw.get("traceback")))
        return Output("text", trace[:MAX_OUTPUT_CHARS])
    if kind in ("execute_result", "display_data"):
        data = raw.get("data") or {}
        if SVG_MIME in data:
            svg = _text(data[SVG_MIME]).encode("utf-8")
            payload = base64.b64encode(svg).decode("ascii")
            return Output("image", src=f"data:{SVG_MIME};base64,{payload}")
        for mime in SAFE_IMAGES:
            if mime in data:
                payload = _text(data[mime]).replace("\n", "")
                try:
                    base64.b64decode(payload, validate=True)
                except (binascii.Error, ValueError):
                    return Output("skipped", "картинка не читается")
                return Output("image", src=f"data:{mime};base64,{payload}")
        if "text/latex" in data:
            tex = _text(data["text/latex"]).strip().strip("$").strip()
            return Output("math", _mathml(tex, display=True))
        if "text/plain" in data:
            return Output("text", _text(data["text/plain"])[:MAX_OUTPUT_CHARS])
        if data:
            return Output("skipped", "вывод в формате " + ", ".join(sorted(data)))
    return None


def parse(content: bytes) -> list[Cell] | None:
    """Разбирает .ipynb. None — файл не разобрался, показывать нечего."""
    try:
        nb = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(nb, dict) or not isinstance(nb.get("cells"), list):
        return None

    language = str(
        ((nb.get("metadata") or {}).get("language_info") or {}).get("name") or "python"
    )
    cells: list[Cell] = []
    for raw in nb["cells"]:
        if not isinstance(raw, dict):
            continue
        source = _text(raw.get("source"))
        kind = raw.get("cell_type")
        if kind == "markdown":
            cells.append(Cell("markdown", html=render_markdown(source)))
        elif kind == "code":
            outputs = [o for o in (_output(r) for r in raw.get("outputs") or []) if o]
            cells.append(
                Cell(
                    "code",
                    html=highlight(source, language),
                    source=source,
                    language=language,
                    outputs=outputs,
                )
            )
        else:
            cells.append(Cell("raw", source=source))
    return cells
