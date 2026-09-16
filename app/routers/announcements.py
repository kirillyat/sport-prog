from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import CurrentUser, SessionDep
from app.models import Announcement, GroupMembership, User, utcnow
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


async def upcoming_for_dashboard(
    session: AsyncSession, user: User, limit: int = 3
) -> list[Announcement]:
    grouped = split_by_state(await list_visible(session, user))
    return (grouped["live"] + grouped["upcoming"])[:limit]


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
        },
    )
