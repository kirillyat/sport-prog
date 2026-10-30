"""Публикация объявления: проверки, запись, рассылка в Telegram.

Здесь, а не в обработчике формы, потому что объявление пишет и нейросеть
по MCP — правила у обоих путей должны быть одни.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app import notify
from app.i18n import translate as _
from app.models import Announcement, Group, User


class AnnouncementError(ValueError):
    """Объявление не опубликовать: сообщение уже переведено и годится для человека."""


async def publish(
    session: AsyncSession,
    author: User,
    *,
    title: str,
    body: str = "",
    url: str = "",
    url_label: str = "",
    group_id: int | None = None,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
    pinned: bool = False,
) -> tuple[Announcement, int]:
    """Опубликованное объявление и сколько сообщений ушло в Telegram."""
    title = title.strip()
    if not title:
        raise AnnouncementError(_("Пустой заголовок"))
    link = url.strip()
    if link and not link.startswith(("http://", "https://")):
        raise AnnouncementError(_("Ссылка должна начинаться с http"))
    if starts_at and ends_at and ends_at <= starts_at:
        raise AnnouncementError(_("Конец раньше начала"))
    if group_id is not None and await session.get(Group, group_id) is None:
        raise AnnouncementError(_("Группа не найдена"))

    item = Announcement(
        title=title,
        body=body.strip() or None,
        url=link or None,
        url_label=url_label.strip() or None,
        group_id=group_id,
        starts_at=starts_at,
        ends_at=ends_at,
        pinned=pinned,
        created_by_id=author.id,
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    delivered = await notify.notify_announcement(item, session)
    return item, delivered
