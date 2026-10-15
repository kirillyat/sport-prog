"""Языки программирования, на которых сдают свои задачи портала.

Список языков знает судья: у judge0 их десятки, и номер у одного и того же
языка в разных сборках разный. Поэтому портал не держит свой справочник,
а спрашивает судью (`GET /languages`), и преподаватель отмечает, какие из
них разрешены студентам. Выбор лежит в служебной таблице рядом с адресом
судьи: миграция ради списка из нескольких чисел не нужна.

Ничего не отмечено — разрешён один язык, `JUDGE0_LANGUAGE_ID` из окружения
(по умолчанию Python 3). Так портал вёл себя до того, как языков стало
несколько, и стенд с идущим курсом после обновления не меняется.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.catalog import get_state, set_state
from app.services.judge import Config, JudgeUnavailable, auth_headers

ALLOWED_KEY = "judge0_languages"
# Что умеет судья — запоминаем, чтобы страница не звонила ему при каждом
# открытии: лёгший судья держал бы её секунды, а именно туда идут его чинить.
OFFERED_KEY = "judge0_offered"


@dataclass(frozen=True, slots=True)
class Language:
    id: int
    # Как язык называет судья: «C++ (GCC 9.2.0)». Это же имя пишется в посылку —
    # преподавателю важна и версия компилятора.
    name: str

    @property
    def family(self) -> str:
        """Язык без версии и компилятора, в нижнем регистре: «c++», «python»."""
        head = self.name.split("(", 1)[0].strip().lower()
        return head.split()[0] if head else ""

    @property
    def editor_mode(self) -> str:
        """Режим CodeMirror. Пусто — редактор без подсветки.

        У нас лежит только режим Python: остальные режимы — отдельные файлы,
        и без них редактор просто не красит код, а писать и сдавать не мешает.
        """
        return "python" if self.family == "python" else ""


DEFAULT_NAME = "Python 3"


def default() -> Language:
    return Language(id=settings.judge0_language_id, name=DEFAULT_NAME)


async def allowed(session: AsyncSession) -> list[Language]:
    """Языки, на которых студент может сдать задачу. Никогда не пусто."""
    return _load(await get_state(session, ALLOWED_KEY)) or [default()]


def _load(raw: str | None) -> list[Language]:
    try:
        items = json.loads(raw) if raw else []
        return [Language(id=int(item["id"]), name=str(item["name"])) for item in items]
    except (ValueError, KeyError, TypeError):
        return []


def _dump(items: list[Language]) -> str:
    return json.dumps([{"id": lang.id, "name": lang.name} for lang in items], ensure_ascii=False)


async def save(session: AsyncSession, chosen: list[Language]) -> None:
    await set_state(session, ALLOWED_KEY, _dump(chosen))


async def cached_offered(session: AsyncSession) -> list[Language]:
    return _load(await get_state(session, OFFERED_KEY))


async def refresh_offered(session: AsyncSession, cfg: Config) -> list[Language]:
    """Спросить судью и запомнить ответ. Не ответил — исключение, запомненное цело."""
    items = await offered(cfg)
    await set_state(session, OFFERED_KEY, _dump(items))
    return items


async def offered(cfg: Config) -> list[Language]:
    """Что умеет судья. Отсортировано по имени, как в списке на странице."""
    if not cfg.ready:
        raise JudgeUnavailable("адрес не задан")
    try:
        async with httpx.AsyncClient(timeout=settings.judge0_ping_timeout) as client:
            response = await client.get(
                f"{cfg.url.rstrip('/')}/languages", headers=auth_headers(cfg)
            )
    except httpx.HTTPError as exc:
        raise JudgeUnavailable(f"судья недоступен: {str(exc) or type(exc).__name__}") from exc
    if response.status_code >= 400:
        raise JudgeUnavailable(f"судья ответил {response.status_code}")
    try:
        items = [Language(id=int(item["id"]), name=str(item["name"])) for item in response.json()]
    except (ValueError, KeyError, TypeError) as exc:
        raise JudgeUnavailable("судья прислал непонятный список языков") from exc
    return sorted(items, key=lambda lang: lang.name.lower())


def pick(choices: list[Language], raw_id: int | None = None, name: str | None = None) -> Language:
    """Язык по номеру из формы или по имени из прошлой посылки.

    Чужой номер — не ошибка, а первый разрешённый язык: язык могли снять,
    пока у студента была открыта страница, и терять из-за этого его код нельзя.
    """
    for lang in choices:
        if raw_id is not None and lang.id == raw_id:
            return lang
    for lang in choices:
        if name and lang.name == name:
            return lang
    return choices[0]


# Как посылки с площадок называют языки: «GNU G++17 7.3.0», «Python3», «PyPy 3».
_ALIASES = (
    (re.compile(r"\+\+"), "cpp"),
    (re.compile(r"\bc#|csharp", re.I), "csharp"),
    (re.compile(r"pypy|python", re.I), "python"),
    (re.compile(r"kotlin", re.I), "kotlin"),
    (re.compile(r"javascript|node", re.I), "javascript"),
    (re.compile(r"typescript", re.I), "typescript"),
    (re.compile(r"\bjava\b", re.I), "java"),
    (re.compile(r"\bgo\b|golang", re.I), "go"),
    (re.compile(r"rust", re.I), "rust"),
    (re.compile(r"pascal|delphi", re.I), "pascal"),
    (re.compile(r"^\s*(gnu\s+)?c\b|\bgcc\b|\bclang\b", re.I), "c"),
)


def lexer_name(language: str | None) -> str:
    """Имя для подсветки Pygments по названию языка из посылки. Пусто — без подсветки."""
    if not language:
        return ""
    for pattern, lexer in _ALIASES:
        if pattern.search(language):
            return lexer
    return ""
