"""Переключатели взгляда: роль, режим администратора и язык интерфейса.

Роль: преподаватель смотрит портал как студент, чтобы проверить, что видят
студенты, не заводя второй аккаунт. Хранится в куке — это про взгляд, а не
про права: страницы жюри остаются доступными по адресу, просто исчезают
из рейки. Язык хранится так же и по той же причине: это настройка браузера,
а не свойство человека в базе.

Режим администратора — тоже кука, но он, наоборот, открывает: без него
настройки портала (судья, модель, видимость разделов, роли) не открываются
и по прямому адресу. Защитой от другого преподавателя это не служит — режим
включает себе любой, — это защита от случайного клика на чужой странице.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.deps import ADMIN_COOKIE, VIEW_COOKIE, CurrentUser
from app.i18n import LANGUAGE_COOKIE, LANGUAGE_COOKIE_MAX_AGE, LANGUAGES

router = APIRouter(prefix="/view", tags=["view"], include_in_schema=False)


def _safe_next(raw: str) -> str:
    """Возвращаемся только на свой путь: «//чужой.сайт» тоже начинается со слэша."""
    return raw if raw.startswith("/") and not raw.startswith("//") else "/"


@router.post("/student")
async def as_student(user: CurrentUser, next: str = Form("/")):
    response = RedirectResponse(_safe_next(next), status_code=303)
    if user.is_teacher:
        response.set_cookie(VIEW_COOKIE, "student", httponly=True, samesite="lax", max_age=86400)
    return response


@router.post("/teacher")
async def as_teacher(request: Request, user: CurrentUser, next: str = Form("/")):
    response = RedirectResponse(_safe_next(next), status_code=303)
    response.delete_cookie(VIEW_COOKIE)
    return response


@router.post("/admin")
async def admin_on(user: CurrentUser, next: str = Form("/")):
    response = RedirectResponse(_safe_next(next), status_code=303)
    if user.is_teacher:
        # Сутки, как и «глазами студента»: в настройки заходят по делу,
        # а назавтра портал снова открывается в обычном, коротком виде.
        response.set_cookie(ADMIN_COOKIE, "on", httponly=True, samesite="lax", max_age=86400)
        # Глазами студента настроек не видно — режим без выхода из этого
        # взгляда ничего бы не показал.
        response.delete_cookie(VIEW_COOKIE)
    return response


@router.post("/admin/off")
async def admin_off(user: CurrentUser, next: str = Form("/")):
    response = RedirectResponse(_safe_next(next), status_code=303)
    response.delete_cookie(ADMIN_COOKIE)
    return response


@router.post("/lang")
async def set_language(lang: str = Form(...), next: str = Form("/")):
    """Язык интерфейса. Доступен и до входа: страницу входа тоже надо читать."""
    response = RedirectResponse(_safe_next(next), status_code=303)
    if lang in LANGUAGES:
        response.set_cookie(
            LANGUAGE_COOKIE,
            lang,
            httponly=True,
            samesite="lax",
            max_age=LANGUAGE_COOKIE_MAX_AGE,
        )
    return response
