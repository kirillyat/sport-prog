"""Переключатель взгляда: преподаватель смотрит портал как студент.

Нужен, чтобы проверить, что видят студенты, не заводя второй аккаунт.
Хранится в куке — это про взгляд, а не про права: страницы жюри остаются
доступными по адресу, просто исчезают из рейки.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.deps import VIEW_COOKIE, CurrentUser

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
