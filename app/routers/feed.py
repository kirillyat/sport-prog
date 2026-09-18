from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import select

from app.deps import AsStudent, CurrentUser, OptionalInt, SessionDep
from app.models import GroupFavorite
from app.services.feed import build_feed, groups_for_feed
from app.templating import templates

router = APIRouter(tags=["feed"])

# «Все группы» в выпадающем списке: ноль отличает осознанный выбор от того,
# что человек просто открыл ленту и ничего не выбирал.
ALL_GROUPS = 0


@router.get("/feed")
async def feed_page(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    as_student: AsStudent,
    group_id: OptionalInt = None,
):
    groups = await groups_for_feed(session, user, as_student)

    # Закреплённые группы — первыми и по умолчанию: лента без выбора открывалась
    # на всех сразу, а преподаватель почти всегда смотрит свою.
    pinned = set(
        (
            await session.execute(
                select(GroupFavorite.group_id).where(GroupFavorite.user_id == user.id)
            )
        ).scalars()
    )
    groups.sort(key=lambda g: (g.id not in pinned, g.title))

    if group_id is None:
        # Группа не выбрана вовсе — открываем закреплённую, если она есть.
        group_id = next((g.id for g in groups if g.id in pinned), None)
    elif group_id == ALL_GROUPS or all(g.id != group_id for g in groups):
        # Ноль — это «все группы», выбранные осознанно; чужая группа — тоже все.
        group_id = None

    items = await build_feed(session, user, group_id=group_id, as_student=as_student)
    return templates.TemplateResponse(
        request,
        "feed.html",
        {"user": user, "items": items, "groups": groups, "group_id": group_id},
    )
