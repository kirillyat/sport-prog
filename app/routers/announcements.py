from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import CurrentUser, SessionDep
from app.i18n import translate as _
from app.models import Announcement, GroupMembership, HiddenAnnouncement, User, utcnow
from app.templating import templates

router = APIRouter(tags=["announcements"])


def visible_announcements_stmt(user: User):
    """Общие объявления плюс объявления групп, в которых состоит пользователь."""
    stmt = select(Announcement)
    if not user.is_teacher:
        my_groups = select(GroupMembership.group_id).where(GroupMembership.user_id == user.id)
        stmt = stmt.where(
            (Announcement.group_id.is_(None)) | (Announcement.group_id.in_(my_groups))
        )
    return stmt


async def list_visible(session: AsyncSession, user: User) -> list[Announcement]:
    stmt = visible_announcements_stmt(user).order_by(
        Announcement.pinned.desc(), Announcement.created_at.desc()
    )
    return list((await session.execute(stmt)).scalars().all())


def split_by_state(items: list[Announcement]) -> dict[str, list[Announcement]]:
    now = utcnow()
    grouped: dict[str, list[Announcement]] = {"live": [], "upcoming": [], "plain": [], "past": []}
    for item in items:
        grouped[item.state(now)].append(item)
    # Ближайшее событие — первым.
    grouped["upcoming"].sort(key=lambda a: a.starts_at)
    grouped["past"].sort(key=lambda a: a.starts_at or a.created_at, reverse=True)
    return grouped


async def hidden_ids(session: AsyncSession, user: User) -> set[int]:
    """Что этот человек убрал с главной."""
    rows = await session.execute(
        select(HiddenAnnouncement.announcement_id).where(HiddenAnnouncement.user_id == user.id)
    )
    return set(rows.scalars().all())


async def upcoming_for_dashboard(
    session: AsyncSession, user: User, limit: int = 3
) -> list[Announcement]:
    """Главная показывает только то, что человек не убрал.

    Список анонсов при этом полный: убранное там на месте, и оттуда же
    возвращается на главную.
    """
    grouped = split_by_state(await list_visible(session, user))
    hidden = await hidden_ids(session, user)
    live_and_next = [a for a in grouped["live"] + grouped["upcoming"] if a.id not in hidden]
    return live_and_next[:limit]


@router.get("/announcements")
async def announcements_page(request: Request, session: SessionDep, user: CurrentUser):
    grouped = split_by_state(await list_visible(session, user))
    return templates.TemplateResponse(
        request,
        "announcements.html",
        {
            "user": user,
            "live": grouped["live"],
            "upcoming": grouped["upcoming"],
            "plain": grouped["plain"],
            "past": grouped["past"],
            "has_any": any(grouped.values()),
            "hidden": await hidden_ids(session, user),
        },
    )


def _back(raw: str) -> str:
    """Возвращаемся туда, откуда нажали: с главной — на главную."""
    return raw if raw.startswith("/") and not raw.startswith("//") else "/"


@router.post("/announcements/{announcement_id}/hide")
async def hide_announcement(
    session: SessionDep, user: CurrentUser, announcement_id: int, next: str = Form("/")
):
    """Убрать объявление с главной. Само объявление остаётся на своей странице."""
    if await session.get(Announcement, announcement_id) is None:
        return RedirectResponse("/?err=" + quote(_("Объявление не найдено")), status_code=303)

    # Второе нажатие не должно падать на уникальном индексе: человек мог
    # открыть главную в двух вкладках.
    stmt = sqlite_insert(HiddenAnnouncement).values(
        user_id=user.id, announcement_id=announcement_id, hidden_at=utcnow()
    )
    await session.execute(stmt.on_conflict_do_nothing())
    await session.commit()
    return RedirectResponse(_back(next), status_code=303)


@router.post("/announcements/{announcement_id}/show")
async def show_announcement(
    session: SessionDep, user: CurrentUser, announcement_id: int, next: str = Form("/announcements")
):
    """Вернуть убранное объявление на главную."""
    row = await session.scalar(
        select(HiddenAnnouncement).where(
            HiddenAnnouncement.user_id == user.id,
            HiddenAnnouncement.announcement_id == announcement_id,
        )
    )
    if row is not None:
        await session.delete(row)
        await session.commit()
    return RedirectResponse(_back(next), status_code=303)
