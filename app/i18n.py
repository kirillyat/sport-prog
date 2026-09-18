"""Переводы интерфейса.

Исходный язык — русский: строка в шаблоне и есть ключ. Перевод ищется
в `app/locales/<язык>.json`; не нашёлся — показываем исходную строку.
Так частичный перевод не ломает страницу, а только оставляет её русской.

Почему не gettext: он требует компиляции `.mo` при сборке и внешнего пакета
ради того же самого. Здесь словарь — обычный JSON, который правится руками
и читается глазами, в том числе тем, кто разворачивает портал у себя.
"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from pathlib import Path

logger = logging.getLogger(__name__)

LOCALES_DIR = Path(__file__).parent / "locales"
DEFAULT_LANGUAGE = "ru"

# Что предлагаем в переключателе. Значение — как язык называет сам себя.
LANGUAGES: dict[str, str] = {
    "ru": "Русский",
    "en": "English",
    "fr": "Français",
}

LANGUAGE_COOKIE = "lang"
LANGUAGE_COOKIE_MAX_AGE = 365 * 24 * 3600

_current: ContextVar[str] = ContextVar("language", default=DEFAULT_LANGUAGE)


def _load() -> dict[str, dict[str, str]]:
    catalogs: dict[str, dict[str, str]] = {}
    for code in LANGUAGES:
        if code == DEFAULT_LANGUAGE:
            continue
        path = LOCALES_DIR / f"{code}.json"
        if not path.is_file():
            continue
        try:
            catalogs[code] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Битый словарь оставляет страницу русской, но не роняет портал.
            logger.exception("не читается словарь %s", path)
    return catalogs


CATALOGS = _load()


def pick_language(cookie: str | None, accept_language: str | None = None) -> str:
    """Выбор человека важнее настроек браузера; браузер подсказывает при первом заходе."""
    if cookie and cookie.lower() in LANGUAGES:
        return cookie.lower()
    for part in (accept_language or "").split(","):
        code = part.split(";")[0].split("-")[0].strip().lower()
        if code in LANGUAGES:
            return code
    return DEFAULT_LANGUAGE


def set_language(code: str) -> None:
    _current.set(code if code in LANGUAGES else DEFAULT_LANGUAGE)


def current_language() -> str:
    return _current.get()


def mark(text: str) -> str:
    """Отметка «эту строку надо перевести», без самого перевода.

    Нужна константам: список разделов или месяцев собирается один раз при
    импорте, когда язык запроса ещё неизвестен. Строка остаётся русской,
    переводится при выводе — `_(SECTIONS[0].title)`, — а сборщик словаря
    видит её здесь и не теряет.
    """
    return text


def translate(text: str) -> str:
    """Перевод строки. Нет перевода — возвращаем исходник, а не пустоту."""
    catalog = CATALOGS.get(_current.get())
    if not catalog:
        return text
    return catalog.get(text) or text
