from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from pydantic import BeforeValidator
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.models import Role, User
from app.security import read_session

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _blank_to_none(value: object) -> object:
    """Пустая строка из <select> — это «все», а не число."""
    return None if value in ("", None) else value


# Необязательный числовой параметр в GET-форме. Без этого выбор «Все»
# в фильтре роняет страницу с ошибкой разбора целого.
OptionalInt = Annotated[int | None, BeforeValidator(_blank_to_none)]


class RedirectToLogin(Exception):
    def __init__(self, next_url: str = "/") -> None:
        self.next_url = next_url


class Forbidden(Exception):
    def __init__(self, message: str = "Недостаточно прав", user: User | None = None) -> None:
        self.message = message
        # Пользователь известен — значит на странице ошибки можно оставить навигацию.
        self.user = user


async def get_optional_user(request: Request, session: SessionDep) -> User | None:
    token = request.cookies.get(settings.session_cookie)
    if not token:
        return None
    user_id = read_session(token)
    if user_id is None:
        return None
    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        return None
    return user


async def require_user(request: Request, session: SessionDep) -> User:
    user = await get_optional_user(request, session)
    if user is None:
        raise RedirectToLogin(str(request.url.path))
    return user


async def require_teacher(request: Request, session: SessionDep) -> User:
    user = await require_user(request, session)
    if user.role != Role.teacher:
        raise Forbidden("Эта страница только для преподавателя", user)
    return user


CurrentUser = Annotated[User, Depends(require_user)]
OptionalUser = Annotated[User | None, Depends(get_optional_user)]
TeacherUser = Annotated[User, Depends(require_teacher)]


def section_required(key: str):
    """Закрытый раздел не должен открываться по прямому адресу — проверяем на сервере."""

    async def guard(session: SessionDep, user: CurrentUser) -> None:
        from app.services import features

        if not features.allows(await features.load(session), key, user):
            raise Forbidden("Раздел сейчас закрыт преподавателем", user)

    return guard

