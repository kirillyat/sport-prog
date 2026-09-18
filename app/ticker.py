"""Бегущая строка ближайшего события — видна с любой страницы.

Считается в middleware один раз на запрос и попадает в шаблоны через
context processor, поэтому роутерам о ней знать не нужно.
"""

from __future__ import annotations

from fastapi import Request
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import Announcement, GroupMembership, HiddenAnnouncement, User, utcnow
from app.security import read_session

SKIP_PREFIXES = ("/static", "/healthz", "/login", "/logout")


async def load_ticker(request: Request) -> Announcement | None:
    if request.method != "GET" or request.url.path.startswith(SKIP_PREFIXES):
        return None
    token = request.cookies.get(settings.session_cookie)
    user_id = read_session(token) if token else None
    if user_id is None:
        return None

    async with SessionLocal() as session:
        user = await session.get(User, user_id)
        if user is None or not user.is_active:
            return None

        stmt = select(Announcement).where(Announcement.starts_at.is_not(None))
        if not user.is_teacher:
            my_groups = select(GroupMembership.group_id).where(GroupMembership.user_id == user.id)
            stmt = stmt.where(
                (Announcement.group_id.is_(None)) | (Announcement.group_id.in_(my_groups))
            )
        # Убранное с главной не должно возвращаться строкой наверху страницы:
        # там оно мешает даже сильнее, чем карточкой.
        hidden = select(HiddenAnnouncement.announcement_id).where(
            HiddenAnnouncement.user_id == user.id
        )
        stmt = stmt.where(Announcement.id.not_in(hidden))
        items = list((await session.execute(stmt)).scalars().all())

    now = utcnow()
    live = [a for a in items if a.state(now) == "live"]
    upcoming = sorted((a for a in items if a.state(now) == "upcoming"), key=lambda a: a.starts_at)
    # Идущее событие важнее будущего; среди будущих — ближайшее.
    return (live or upcoming or [None])[0]


def ticker_context(request: Request) -> dict:
    return {"ticker": getattr(request.state, "ticker", None)}
