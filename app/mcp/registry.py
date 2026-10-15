"""Реестр инструментов, которые портал отдаёт нейросетям.

Инструмент — обычная асинхронная функция над сервисами портала: на входе
контекст (кто спрашивает) и pydantic-модель параметров, на выходе словарь.
Транспорта здесь нет. MCP по HTTP (`app/mcp/server.py`) — один способ
вызвать инструмент; встроенная модель портала, когда появится, будет звать
тот же `call` напрямую, без сети и без второй копии правил доступа.

Поэтому права проверяются здесь, а не в транспорте: роль, ключ на запись
и видимость раздела. Транспорт только узнаёт, кто пришёл.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, get_type_hints

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Role, User

logger = logging.getLogger(__name__)

STUDENT = frozenset({Role.student, Role.teacher})
# Только студенту: «мои оценки» у преподавателя пусты и только сбивали бы модель.
STUDENT_ONLY = frozenset({Role.student})
TEACHER = frozenset({Role.teacher})


class ToolError(Exception):
    """Отказ, понятный модели: нет такого задания, не хватает прав, пустое название.

    Возвращается как результат с `isError`, а не как сбой протокола, — так
    модель видит причину и может исправиться сама.
    """


@dataclass(slots=True)
class Context:
    session: AsyncSession
    user: User
    # Ключ разрешает менять портал. Без него инструменты записи не видны вовсе.
    can_write: bool = False
    # Разделы портала, открытые этому человеку (`features`). Пусто по умолчанию:
    # забыл загрузить — инструменты закрытых разделов не видны, а не наоборот.
    sections: frozenset[str] = frozenset()


Handler = Callable[[Context, Any], Awaitable[dict]]


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    # Короткая подпись для людей: в списке на странице «ИИ» и в клиентах MCP.
    title: str
    # Строка для человека на странице «ИИ»: что инструмент даёт. Описание
    # для модели длинное, написано для неё и не переводится — людям нужна своя.
    hint: str
    description: str
    params: type[BaseModel]
    handler: Handler
    roles: frozenset[Role]
    writes: bool = False
    # Действие видно за пределами портала: например, сообщение группе в Telegram.
    # Клиенты MCP по этой отметке переспрашивают человека.
    notifies: bool = False
    # Раздел портала, которому инструмент принадлежит. Закрыт раздел для роли
    # в «Видимости разделов» — закрыт и инструмент: страница и нейросеть не
    # должны показывать разное.
    section: str | None = None

    def open_in(self, sections: frozenset[str]) -> bool:
        return self.section is None or self.section in sections

    def allowed(self, ctx: Context) -> bool:
        return (
            ctx.user.role in self.roles
            and (ctx.can_write or not self.writes)
            and self.open_in(ctx.sections)
        )

    def schema(self) -> dict:
        """Описание для клиента MCP.

        Схема параметров — из той же модели, по которой они проверяются,
        поэтому описание и проверка не расходятся.
        """
        input_schema = self.params.model_json_schema()
        input_schema.pop("title", None)
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": input_schema,
            "annotations": {
                "title": self.title,
                "readOnlyHint": not self.writes,
                "destructiveHint": False,
                "idempotentHint": not self.writes,
                "openWorldHint": self.notifies,
            },
        }


TOOLS: dict[str, Tool] = {}


def tool(
    name: str,
    *,
    title: str,
    hint: str,
    roles: frozenset[Role],
    writes: bool = False,
    notifies: bool = False,
    section: str | None = None,
):
    """Регистрирует инструмент. Описание для модели — докстрока функции.

    Параметры — аннотация второго аргумента: одна модель и для схемы,
    и для проверки входа.
    """

    def register(fn: Handler) -> Handler:
        params = get_type_hints(fn)["args"]
        TOOLS[name] = Tool(
            name=name,
            title=title,
            hint=hint,
            description=inspect.cleandoc(fn.__doc__ or ""),
            params=params,
            handler=fn,
            roles=roles,
            writes=writes,
            notifies=notifies,
            section=section,
        )
        return fn

    return register


def for_role(role: Role, sections: frozenset[str]) -> list[Tool]:
    """Всё, что роль может получить, включая инструменты записи, — для
    страницы «ИИ». Порядок тот же, что у `available`."""
    return [item for item in TOOLS.values() if role in item.roles and item.open_in(sections)]


def available(ctx: Context) -> list[Tool]:
    """Что этому человеку с этим ключом можно звать. Порядок постоянный:
    клиенты кэшируют список, а модели — начало подсказки."""
    return [item for item in TOOLS.values() if item.allowed(ctx)]


SECTION = "ai"


async def context_for(session: AsyncSession, user: User, *, can_write: bool = False) -> Context:
    """Контекст вызова с открытыми человеку разделами. Собирать его только так:
    иначе инструменты закрытых разделов стали бы невидимы и для открытых."""
    from app.services import features

    sections = frozenset(features.visible(await features.load(session), user))
    return Context(session=session, user=user, can_write=can_write, sections=sections)


def section_open(ctx: Context) -> bool:
    """Раздел «ИИ» закрыт этой роли — закрыт и доступ по ключам.

    Так преподаватель одной галочкой выключает нейросети студентам, например
    на время контрольной, не отзывая ключи по одному.
    """
    return SECTION in ctx.sections


def _validation_message(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        where = ".".join(str(part) for part in error["loc"]) or "параметры"
        parts.append(f"{where}: {error['msg']}")
    return "Неверные параметры — " + "; ".join(parts)


async def call(ctx: Context, name: str, arguments: dict | None) -> dict:
    """Проверить права и параметры, выполнить. ToolError — отказ для модели."""
    item = TOOLS.get(name)
    # Чужой инструмент неотличим от несуществующего: незачем подсказывать
    # студенту, что у преподавателя есть выгрузка решений.
    if item is None or ctx.user.role not in item.roles:
        raise ToolError(f"Инструмента {name!r} нет")
    if not item.open_in(ctx.sections):
        raise ToolError("Этот раздел портала сейчас закрыт преподавателем")
    if item.writes and not ctx.can_write:
        raise ToolError(
            "Ключ выпущен только на чтение. Чтобы менять портал, выпусти на странице "
            "«ИИ» ключ с разрешением на изменения."
        )
    try:
        args = item.params.model_validate(arguments or {})
    except ValidationError as exc:
        raise ToolError(_validation_message(exc)) from exc
    return await item.handler(ctx, args)
