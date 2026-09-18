from __future__ import annotations

import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.i18n import translate as _
from app.models import Platform, PlatformAccount, User, utcnow
from app.platforms import CodeforcesClient, LeetCodeClient
from app.platforms.base import PlatformError, RemoteProfile, UserNotFound

CODE_PREFIX = "msu-sport-"


def new_verification_code() -> str:
    return CODE_PREFIX + secrets.token_hex(4)


WHERE_TO_PUT = {
    Platform.codeforces: (
        _("Зайди в Codeforces → Settings → Social, впиши код в поле «Organization» "
          "(или во «First name») и сохрани.")
    ),
    Platform.leetcode: (
        _("Зайди в LeetCode → Profile → Edit Profile, впиши код в поле «Name» "
          "(или в «Summary»/«Website») и сохрани.")
    ),
}


async def fetch_profile(platform: Platform, handle: str) -> RemoteProfile:
    if platform == Platform.codeforces:
        async with CodeforcesClient() as client:
            return await client.fetch_profile(handle)
    async with LeetCodeClient() as client:
        return await client.fetch_profile(handle)


async def start_verification(
    session: AsyncSession, user: User, platform: Platform, handle: str
) -> PlatformAccount:
    """Создаёт или перепривязывает аккаунт и выдаёт код для подтверждения."""
    handle = handle.strip().strip("@")
    if not handle:
        raise ValueError(_("Пустой хэндл"))

    # Проверяем, что такой пользователь на платформе вообще существует,
    # чтобы студент не ждал верификации опечатки.
    try:
        await fetch_profile(platform, handle)
    except UserNotFound as exc:
        raise ValueError(
            _("На %(platform)s нет пользователя «%(handle)s»")
            % {"platform": platform.title, "handle": handle}
        ) from exc
    except PlatformError as exc:
        raise ValueError(
            _("%(platform)s сейчас недоступен: %(why)s") % {"platform": platform.title, "why": exc}
        ) from exc

    taken = await session.scalar(
        select(PlatformAccount).where(
            PlatformAccount.platform == platform,
            PlatformAccount.handle == handle,
            PlatformAccount.verified_at.is_not(None),
            PlatformAccount.user_id != user.id,
        )
    )
    if taken is not None:
        raise ValueError(
            _("Аккаунт «%(handle)s» уже привязан к другому студенту") % {"handle": handle}
        )

    account = await session.scalar(
        select(PlatformAccount).where(
            PlatformAccount.user_id == user.id, PlatformAccount.platform == platform
        )
    )
    if account is None:
        account = PlatformAccount(user_id=user.id, platform=platform, handle=handle)
        session.add(account)
    else:
        account.handle = handle
        account.verified_at = None
        account.last_sync_error = None

    account.verification_code = new_verification_code()
    await session.commit()
    await session.refresh(account)
    return account


async def confirm_verification(session: AsyncSession, account: PlatformAccount) -> bool:
    if account.is_verified:
        return True
    if not account.verification_code:
        raise ValueError(_("Сначала запроси код верификации"))

    try:
        profile = await fetch_profile(account.platform, account.handle)
    except UserNotFound as exc:
        raise ValueError(
            _("Профиль «%(handle)s» больше не найден") % {"handle": account.handle}
        ) from exc
    except PlatformError as exc:
        raise ValueError(
            _("%(platform)s сейчас недоступен: %(why)s")
            % {"platform": account.platform.title, "why": exc}
        ) from exc

    if account.verification_code.lower() not in profile.searchable_text.lower():
        return False

    account.verified_at = utcnow()
    account.verification_code = None
    await session.commit()
    return True
