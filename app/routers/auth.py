from __future__ import annotations

import secrets
from datetime import timedelta
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select

from app import oidc
from app.config import settings
from app.deps import CurrentUser, OptionalUser, SessionDep, get_optional_user
from app.i18n import translate as _
from app.models import LoginToken, Role, User, utcnow
from app.security import issue_session
from app.templating import templates

router = APIRouter(tags=["auth"])


def _set_session_cookie(response: RedirectResponse, user_id: int) -> RedirectResponse:
    response.set_cookie(
        settings.session_cookie,
        issue_session(user_id),
        max_age=settings.session_ttl_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.base_url.startswith("https://"),
        path="/",
    )
    return response


async def _is_first_user(session: SessionDep) -> bool:
    """Первый вошедший становится преподавателем.

    Иначе портал запирается: переключатель ролей лежит на странице,
    которая сама требует роли преподавателя.
    """
    return (await session.scalar(select(func.count()).select_from(User))) == 0


async def _resolve_user(session: SessionDep, token: LoginToken) -> User:
    user = await session.scalar(select(User).where(User.telegram_id == token.telegram_id))
    if user is None:
        first = await _is_first_user(session)
        role = (
            Role.teacher
            if (first or token.telegram_id in settings.teacher_ids)
            else Role.student
        )
        user = User(
            display_name=token.display_name or f"tg{token.telegram_id}",
            telegram_id=token.telegram_id,
            telegram_username=token.telegram_username,
            role=role,
        )
        session.add(user)
    else:
        user.telegram_username = token.telegram_username
        # Список преподавателей в .env — источник истины, применяем при каждом входе.
        if token.telegram_id in settings.teacher_ids:
            user.role = Role.teacher
    await session.commit()
    await session.refresh(user)
    return user


async def _resolve_oidc_user(session: SessionDep, claims: dict, groups: set[str]) -> User:
    sub = str(claims["sub"])
    user = await session.scalar(select(User).where(User.oidc_sub == sub))
    teacher_by_group = bool(groups & settings.oidc_teacher_group_set)
    if user is None:
        first = await _is_first_user(session)
        user = User(
            display_name=oidc.display_name_from(claims),
            email=claims.get("email"),
            oidc_sub=sub,
            role=Role.teacher if (first or teacher_by_group) else Role.student,
        )
        session.add(user)
    else:
        user.email = claims.get("email") or user.email
        # Группа провайдера — источник истины на повышение. Понижать автоматически
        # не станем: преподаватель мог получить роль иначе (первый вход, вручную).
        if teacher_by_group:
            user.role = Role.teacher
    await session.commit()
    await session.refresh(user)
    return user


@router.get("/login/telegram/link")
async def link_telegram_start(request: Request, session: SessionDep, user: CurrentUser):
    """Привязка Telegram к уже существующей учётке (например, входу через OIDC)."""
    if not settings.telegram_enabled:
        message = quote(_("Telegram-бот не настроен"))
        return RedirectResponse(f"/accounts?err={message}", status_code=303)

    token = LoginToken(
        code=secrets.token_urlsafe(9).replace("-", "_"),
        expires_at=utcnow() + timedelta(seconds=settings.login_code_ttl_seconds),
        link_user_id=user.id,
    )
    session.add(token)
    await session.commit()
    return templates.TemplateResponse(
        request,
        "link_telegram.html",
        {
            "user": user,
            "code": token.code,
            "deep_link": settings.telegram_login_url_template.format(code=token.code),
        },
    )


@router.get("/login/oidc")
async def login_oidc_start(request: Request, user: OptionalUser, link: int = 0):
    # link=1 — привязать провайдера к текущей учётке, а не входить заново.
    linking = bool(link) and user is not None
    if user is not None and not linking:
        return RedirectResponse("/", status_code=303)
    if not settings.oidc_enabled:
        return RedirectResponse("/login?err=" + quote(_("OIDC-вход не настроен")), status_code=303)
    try:
        disc = await oidc.discover()
    except oidc.OIDCError as exc:
        message = quote(f"{settings.oidc_provider_name}: {exc}")
        return RedirectResponse(f"/login?err={message}", status_code=303)

    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    verifier, challenge = oidc.pkce_pair()
    next_url = request.query_params.get("next", "/")
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = "/"

    response = RedirectResponse(
        oidc.authorization_url(disc, state=state, nonce=nonce, code_challenge=challenge),
        status_code=303,
    )
    response.set_cookie(
        oidc.STATE_COOKIE,
        oidc.pack_state({
            "state": state, "nonce": nonce, "verifier": verifier,
            "next": "/accounts" if linking else next_url,
            "link_user_id": user.id if linking else None,
        }),
        max_age=oidc.STATE_TTL,
        httponly=True,
        samesite="lax",
        secure=settings.base_url.startswith("https://"),
        path="/login/oidc",
    )
    return response


@router.get("/login/oidc/callback")
async def login_oidc_callback(request: Request, session: SessionDep):
    def fail(message: str) -> RedirectResponse:
        response = RedirectResponse("/login?err=" + quote(message), status_code=303)
        response.delete_cookie(oidc.STATE_COOKIE, path="/login/oidc")
        return response

    if error := request.query_params.get("error"):
        detail = request.query_params.get("error_description") or error
        return fail(f"{settings.oidc_provider_name}: {detail}")

    saved = oidc.unpack_state(request.cookies.get(oidc.STATE_COOKIE))
    if saved is None or request.query_params.get("state") != saved.get("state"):
        return fail(_("Сессия входа устарела, попробуй ещё раз"))
    code = request.query_params.get("code")
    if not code:
        return fail(_("Провайдер не вернул код"))

    try:
        disc = await oidc.discover()
        tokens = await oidc.exchange_code(disc, code, saved["verifier"])
        claims = oidc.id_token_claims(tokens["id_token"])
    except oidc.OIDCError as exc:
        return fail(f"{settings.oidc_provider_name}: {exc}")

    if claims.get("nonce") != saved.get("nonce"):
        return fail(_("Ответ провайдера не совпал с запросом"))
    if "sub" not in claims:
        return fail(_("В токене нет идентификатора пользователя"))

    userinfo = await oidc.fetch_userinfo(disc, tokens.get("access_token", ""))
    merged = {**claims, **userinfo}
    groups = oidc.groups_from(merged)

    link_user_id = saved.get("link_user_id")
    if link_user_id is not None:
        current = await get_optional_user(request, session)
        if current is None or current.id != link_user_id:
            return fail(_("Войди и начни привязку заново"))
        taken = await session.scalar(
            select(User).where(User.oidc_sub == str(merged["sub"]), User.id != current.id)
        )
        if taken is not None:
            return fail(
                _("Этот аккаунт %(provider)s уже привязан к другому участнику")
                % {"provider": settings.oidc_provider_name}
            )
        current.oidc_sub = str(merged["sub"])
        current.email = merged.get("email") or current.email
        if groups & settings.oidc_teacher_group_set:
            current.role = Role.teacher
        await session.commit()
        ok = quote(_("%(provider)s привязан") % {"provider": settings.oidc_provider_name})
        response = RedirectResponse(f"/accounts?ok={ok}", status_code=303)
        response.delete_cookie(oidc.STATE_COOKIE, path="/login/oidc")
        return response

    user = await _resolve_oidc_user(session, merged, groups)
    target = RedirectResponse(saved.get("next") or "/", status_code=303)
    response = _set_session_cookie(target, user.id)
    response.delete_cookie(oidc.STATE_COOKIE, path="/login/oidc")
    return response


@router.get("/login")
async def login_page(request: Request, session: SessionDep, user: OptionalUser):
    if user is not None:
        return RedirectResponse("/", status_code=303)

    token = None
    if settings.telegram_enabled:
        token = LoginToken(
            code=secrets.token_urlsafe(9).replace("-", "_"),
            expires_at=utcnow() + timedelta(seconds=settings.login_code_ttl_seconds),
        )
        session.add(token)
        await session.commit()

    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "code": token.code if token else None,
            "deep_link": settings.telegram_login_url_template.format(code=token.code)
            if token
            else None,
            "dev_login": settings.dev_login_enabled,
            "oidc_enabled": settings.oidc_enabled,
            "oidc_name": settings.oidc_provider_name,
            "next": request.query_params.get("next", "/"),
            "error": request.query_params.get("err"),
        },
    )


@router.get("/login/status/{code}")
async def login_status(code: str, session: SessionDep):
    token = await session.scalar(select(LoginToken).where(LoginToken.code == code))
    if token is None:
        return JSONResponse({"state": "unknown"})
    if token.consumed_at is not None:
        return JSONResponse({"state": "consumed"})
    if token.expires_at < utcnow():
        return JSONResponse({"state": "expired"})
    if token.confirmed_at is not None:
        return JSONResponse({"state": "confirmed", "next": f"/login/complete/{code}"})
    return JSONResponse({"state": "pending"})


@router.get("/login/complete/{code}")
async def login_complete(request: Request, code: str, session: SessionDep):
    token = await session.scalar(select(LoginToken).where(LoginToken.code == code))
    now = utcnow()
    if (
        token is None
        or token.confirmed_at is None
        or token.consumed_at is not None
        or token.expires_at < now
        or token.telegram_id is None
    ):
        return RedirectResponse("/login?err=" + quote(_("Код недействителен")), status_code=303)

    if token.link_user_id is not None:
        # Привязку завершает тот же браузер, что её начал, — иначе чужой код
        # мог бы прицепить чужой Telegram к этой учётке.
        current = await get_optional_user(request, session)
        if current is None or current.id != token.link_user_id:
            # Неавторизованного /accounts всё равно отправит на вход — ведём сразу туда.
            message = quote(_("Войди и начни привязку заново"))
            target = "/accounts" if current else "/login"
            return RedirectResponse(f"{target}?err={message}", status_code=303)

        taken = await session.scalar(
            select(User).where(User.telegram_id == token.telegram_id, User.id != current.id)
        )
        if taken is not None:
            return RedirectResponse(
                "/accounts?err=" + quote(_("Этот Telegram уже привязан к другому участнику")),
                status_code=303,
            )
        current.telegram_id = token.telegram_id
        current.telegram_username = token.telegram_username
        token.consumed_at = now
        await session.commit()
        return RedirectResponse("/accounts?ok=" + quote(_("Telegram привязан")), status_code=303)

    token.consumed_at = now
    user = await _resolve_user(session, token)
    await session.commit()
    return _set_session_cookie(RedirectResponse("/", status_code=303), user.id)


@router.post("/login/dev")
async def login_dev(session: SessionDep, name: str = Form(...), teacher: bool = Form(False)):
    """Вход без Telegram. Работает только при DEV_LOGIN_ENABLED=true."""
    if not settings.dev_login_enabled:
        return RedirectResponse("/login?err=" + quote(_("Dev-вход выключен")), status_code=303)

    name = name.strip()
    if not name:
        return RedirectResponse("/login?err=" + quote(_("Введите имя")), status_code=303)

    user = await session.scalar(select(User).where(User.display_name == name))
    if user is None:
        first = await _is_first_user(session)
        user = User(display_name=name, role=Role.teacher if (teacher or first) else Role.student)
        session.add(user)
        await session.commit()
        await session.refresh(user)
    elif teacher and user.role != Role.teacher:
        user.role = Role.teacher
        await session.commit()

    return _set_session_cookie(RedirectResponse("/", status_code=303), user.id)


@router.post("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(settings.session_cookie, path="/")
    return response
