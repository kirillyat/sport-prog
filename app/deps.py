from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from pydantic import BeforeValidator
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.i18n import translate as _
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
    def __init__(
        self, message: str = "", user: User | None = None, *, admin_next: str | None = None
    ) -> None:
        # Пустое сообщение переводится на странице ошибки: язык известен
        # там, а не в момент, когда исключение поднялось.
        self.message = message
        # Пользователь известен — значит на странице ошибки можно оставить навигацию.
        self.user = user
        # Отказ из-за выключенного режима администратора: страница ошибки
        # предлагает включить его и вернуться сюда, а не искать переключатель.
        self.admin_next = admin_next


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
        raise Forbidden(_("Эта страница только для преподавателя"), user)
    return user


VIEW_COOKIE = "view_as"
ADMIN_COOKIE = "admin_mode"


def viewing_as_student(request: Request) -> bool:
    """Преподаватель попросил показать портал глазами студента.

    Права это не меняет: страницы жюри по-прежнему открываются по прямому
    адресу. Меняется то, что портал показывает и как считает «своих».
    """
    return request.cookies.get(VIEW_COOKIE) == "student"


def admin_mode(request: Request) -> bool:
    """Преподаватель включил себе режим администратора.

    Это не права, а порядок на экране: любой преподаватель включает режим
    сам, в меню под аватаркой. По умолчанию судья, ключи модели, видимость
    разделов и выдача ролей убраны с глаз — каждый день они не нужны, а
    случайная правка там бьёт по всем группам сразу.
    """
    return request.cookies.get(ADMIN_COOKIE) == "on"


async def require_admin(request: Request, session: SessionDep) -> User:
    user = await require_teacher(request, session)
    if not admin_mode(request):
        # Действия — POST, и возвращаться после включения режима некуда:
        # такой адрес по GET не открывается. Тогда — на панель.
        back = request.url.path if request.method == "GET" else "/teacher"
        raise Forbidden(
            _("Это настройки администратора. Включи режим администратора, чтобы их открыть"),
            user,
            admin_next=back,
        )
    return user


AsStudent = Annotated[bool, Depends(viewing_as_student)]
CurrentUser = Annotated[User, Depends(require_user)]
OptionalUser = Annotated[User | None, Depends(get_optional_user)]
TeacherUser = Annotated[User, Depends(require_teacher)]
AdminUser = Annotated[User, Depends(require_admin)]


def section_required(key: str):
    """Закрытый раздел не должен открываться по прямому адресу — проверяем на сервере."""

    async def guard(session: SessionDep, user: CurrentUser) -> None:
        from app.services import features

        if not features.allows(await features.load(session), key, user):
            raise Forbidden(_("Раздел сейчас закрыт преподавателем"), user)

    return guard

