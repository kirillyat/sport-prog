from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.access import is_confirmed
from app.config import settings
from app.deps import CurrentUser, SessionDep
from app.i18n import translate as _
from app.models import Platform, PlatformAccount
from app.services import verification
from app.services.sync import sync_account
from app.templating import templates

router = APIRouter(prefix="/accounts", tags=["accounts"])


def _back(
    message: str | None = None, error: str | None = None, back: str = "/accounts"
) -> RedirectResponse:
    params = []
    if message:
        params.append(f"ok={message}")
    if error:
        params.append(f"err={error}")
    suffix = ("?" + "&".join(params)) if params else ""
    # Адрес возврата приходит из формы, поэтому принимаем только свой путь:
    # «//чужой.сайт» тоже начинается со слэша.
    target = back if back.startswith("/") and not back.startswith("//") else "/accounts"
    return RedirectResponse(f"{target}{suffix}", status_code=303)


@router.get("")
async def accounts_page(request: Request, user: CurrentUser):
    return templates.TemplateResponse(
        request,
        "accounts.html",
        {
            "user": user,
            "confirmed": is_confirmed(user),
            "platforms": Platform.external(),
            "where_to_put": verification.WHERE_TO_PUT,
            "ok": request.query_params.get("ok"),
            "error": request.query_params.get("err"),
        },
    )


@router.post("/telegram/unlink")
async def unlink_telegram(session: SessionDep, user: CurrentUser):
    """Отвязать Telegram можно, только когда вход остаётся по учётной записи вуза."""
    if user.telegram_id is None:
        return _back(error=_("Telegram не привязан"))
    if user.oidc_sub is None:
        return _back(
            error=_("Сначала привяжи %(provider)s — иначе входить будет нечем")
            % {"provider": settings.oidc_provider_name}
        )
    user.telegram_id = None
    user.telegram_username = None
    await session.commit()
    return _back(message=_("Telegram отвязан"))


@router.post("/oidc/unlink")
async def unlink_oidc(session: SessionDep, user: CurrentUser):
    """Учётная запись вуза не отвязывается: она и есть подтверждение студенчества.

    Отвязали бы — человек остался бы в группах, перестав быть подтверждённым.
    """
    if user.oidc_sub is None:
        return _back(
        error=_("%(provider)s не привязан") % {"provider": settings.oidc_provider_name}
    )
    return _back(
        error=_("%(provider)s подтверждает, что ты %(who)s — отвязать нельзя")
        % {"provider": settings.oidc_provider_name, "who": settings.org_student}
    )


@router.post("/link")
async def link_account(
    session: SessionDep,
    user: CurrentUser,
    platform: str = Form(...),
    handle: str = Form(...),
    back: str = Form("/accounts"),
):
    try:
        target = Platform(platform)
    except ValueError:
        return _back(error=_("Неизвестная платформа"), back=back)

    try:
        await verification.start_verification(session, user, target, handle)
    except ValueError as exc:
        return _back(error=str(exc))
    return _back(message=_("Код выдан, впиши его в профиль и нажми «Проверить»"), back=back)


@router.post("/{account_id}/verify")
async def verify_account(
    session: SessionDep, user: CurrentUser, account_id: int,
    back: str = Form("/accounts"),
):
    account = await session.get(PlatformAccount, account_id)
    if account is None or account.user_id != user.id:
        return _back(error=_("Аккаунт не найден"), back=back)
    try:
        confirmed = await verification.confirm_verification(session, account)
    except ValueError as exc:
        return _back(error=str(exc))
    if not confirmed:
        return _back(error=_("Код в профиле не найден. Сохранил ли ты изменения?"), back=back)

    await sync_account(session, account)
    return _back(
        message=_("%(platform)s привязан, посылки загружаются")
        % {"platform": account.platform.title},
        back=back,
    )


@router.post("/{account_id}/sync")
async def sync_now(
    session: SessionDep, user: CurrentUser, account_id: int,
    back: str = Form("/accounts"),
):
    account = await session.get(PlatformAccount, account_id)
    if account is None or account.user_id != user.id:
        return _back(error=_("Аккаунт не найден"), back=back)
    if not account.is_verified:
        return _back(error=_("Сначала подтверди аккаунт"), back=back)
    added = await sync_account(session, account)
    if account.last_sync_error:
        return _back(error=account.last_sync_error, back=back)
    return _back(
        message=_("Синхронизировано, новых посылок: %(count)s") % {"count": added}, back=back
    )


@router.post("/{account_id}/unlink")
async def unlink_account(
    session: SessionDep, user: CurrentUser, account_id: int,
    back: str = Form("/accounts"),
):
    account = await session.get(PlatformAccount, account_id)
    if account is None or account.user_id != user.id:
        return _back(error=_("Аккаунт не найден"), back=back)
    await session.delete(account)
    await session.commit()
    return _back(message=_("Аккаунт отвязан"), back=back)


@router.get("/handles/{platform}")
async def taken_handles(session: SessionDep, user: CurrentUser, platform: str):
    """Служебный эндпоинт: какие хэндлы уже заняты (чтобы не гадать при привязке)."""
    try:
        target = Platform(platform)
    except ValueError:
        return {"handles": []}
    rows = await session.execute(
        select(PlatformAccount.handle).where(
            PlatformAccount.platform == target, PlatformAccount.verified_at.is_not(None)
        )
    )
    return {"handles": sorted(rows.scalars().all())}
