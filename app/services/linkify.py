"""Текст человека — в HTML со ссылками. Без зависимостей от остального
портала: им пользуются и шаблоны (фильтр `linkify`), и сервисы."""

from __future__ import annotations

import re
from html import escape

from markupsafe import Markup

# Только http и https: javascript: и прочее в ссылку не превращаем.
URL = re.compile(r"https?://[^\s<>\"']+")
# Точка или скобка в конце фразы — не часть адреса.
TRAILING = ".,;:!?)»”"


def links(body: str) -> list[str]:
    return [match.group().rstrip(TRAILING) for match in URL.finditer(body)]


def first_link(body: str) -> str | None:
    """Первая ссылка — под неё рисуем кнопку: по кнопке на паре попадают
    быстрее, чем по ссылке посреди текста."""
    found = links(body)
    return found[0] if found else None


def render(body: str) -> Markup:
    """Текст сообщения в HTML: всё экранировано, ссылки кликабельны и
    открываются в новой вкладке, переносы строк сохранены."""
    out: list[str] = []
    position = 0
    for match in URL.finditer(body):
        url = match.group().rstrip(TRAILING)
        out.append(escape(body[position:match.start()]))
        out.append(
            f'<a href="{escape(url)}" target="_blank" rel="noopener noreferrer">{escape(url)}</a>'
        )
        position = match.start() + len(url)
    out.append(escape(body[position:]))
    return Markup("".join(out).replace("\n", "<br>"))


