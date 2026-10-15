"""Ключи для нейросетей: выпуск, проверка, отзыв.

Ключ — случайная строка, в базе лежит её sha256. Медленный хэш вроде
bcrypt тут не нужен: перебирать нечего, в ключе 256 бит случайности,
а не придуманный человеком пароль.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ApiToken, User, utcnow

PREFIX = "sport_"
# Больше ключей человеку не нужно: по одному на каждую программу с запасом.
# Предел нужен, чтобы забытые ключи не копились десятками.
MAX_PER_USER = 10
# Отметку «последний раз пользовались» обновляем не на каждом запросе:
# агент шлёт их десятками подряд, а писать в SQLite ради минутной точности незачем.
TOUCH_EVERY = timedelta(minutes=5)


def _digest(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def for_user(session: AsyncSession, user: User) -> list[ApiToken]:
    stmt = select(ApiToken).where(ApiToken.user_id == user.id).order_by(ApiToken.created_at)
    return list((await session.execute(stmt)).scalars().all())


async def issue(
    session: AsyncSession, user: User, name: str, *, can_write: bool
) -> tuple[ApiToken, str] | None:
    """Новый ключ и его открытый текст — показать один раз. None — предел исчерпан.

    Писать разрешаем только преподавателю: студенту портал через MCP ничего
    менять не даёт, и флаг у его ключа ничего бы не значил, кроме путаницы.
    """
    count = await session.scalar(
        select(func.count()).select_from(ApiToken).where(ApiToken.user_id == user.id)
    )
    if (count or 0) >= MAX_PER_USER:
        return None
    raw = PREFIX + secrets.token_urlsafe(32)
    token = ApiToken(
        user_id=user.id,
        name=name.strip()[:80] or "MCP",
        token_hash=_digest(raw),
        hint=raw[: len(PREFIX) + 4],
        can_write=can_write and user.is_teacher,
    )
    session.add(token)
    await session.commit()
    await session.refresh(token)
    return token, raw


async def revoke(session: AsyncSession, user: User, token_id: int) -> bool:
    token = await session.get(ApiToken, token_id)
    if token is None or token.user_id != user.id:
        return False
    await session.delete(token)
    await session.commit()
    return True


async def authenticate(session: AsyncSession, raw: str) -> tuple[ApiToken, User] | None:
    """Ключ из заголовка → ключ и владелец. Отключённый человек не входит и с ключом."""
    raw = raw.strip()
    if not raw.startswith(PREFIX):
        return None
    token = await session.scalar(select(ApiToken).where(ApiToken.token_hash == _digest(raw)))
    if token is None:
        return None
    user = await session.get(User, token.user_id)
    if user is None or not user.is_active:
        return None
    now = utcnow()
    if token.last_used_at is None or now - token.last_used_at > TOUCH_EVERY:
        token.last_used_at = now
        await session.commit()
    return token, user
