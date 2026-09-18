from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Request
from sqlalchemy import select

from app.deps import AsStudent, CurrentUser, OptionalInt, SessionDep
from app.models import Assignment, Group, GroupFavorite, utcnow
from app.services.leaderboard import build_leaderboard
from app.services.progress import groups_for_user
from app.templating import templates

router = APIRouter(tags=["leaderboard"])

PERIODS = {
    "all": ("За всё время", None),
    "month": ("За 30 дней", 30),
    "week": ("За неделю", 7),
}


@router.get("/leaderboard")
async def leaderboard_page(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    as_student: AsStudent,
    group_id: OptionalInt = None,
    period: str = "all",
):
    if period not in PERIODS:
        period = "all"
    _, days = PERIODS[period]
    since = utcnow() - timedelta(days=days) if days else None

    if user.is_teacher and not as_student:
        stmt = select(Group).where(Group.is_archived.is_(False)).order_by(Group.title)
        groups = list((await session.execute(stmt)).scalars().all())
    else:
        groups = await groups_for_user(session, user)

    # Закреплённые группы идут первыми: раз преподаватель их закрепил, табло
    # должно открываться на них, а не на первой по алфавиту.
    pinned = set(
        (
            await session.execute(
                select(GroupFavorite.group_id).where(GroupFavorite.user_id == user.id)
            )
        ).scalars()
    )
    groups.sort(key=lambda g: (g.id not in pinned, g.title))

    # Общего табло нет: сравнивать студентов из разных групп не за что —
    # задания у них разные. Не выбрана группа или выбрана чужая — берём первую,
    # то есть закреплённую, если она есть.
    if group_id is None or all(g.id != group_id for g in groups):
        group_id = groups[0].id if groups else None

    rows = await build_leaderboard(session, group_id=group_id, since=since) if group_id else []

    # Табло отвечает «кто впереди», но не «за что» — поэтому рядом список
    # заданий группы. Преподавателю почти всегда нужен переход в конкретное.
    assignments = []
    if user.is_teacher and not as_student and group_id is not None:
        assignments = list(
            (
                await session.execute(
                    select(Assignment)
                    .where(Assignment.group_id == group_id)
                    .order_by(Assignment.assigned_at.desc())
                )
            ).scalars()
        )
    my_place = next((i + 1 for i, r in enumerate(rows) if r.user.id == user.id), None)

    return templates.TemplateResponse(
        request,
        "leaderboard.html",
        {
            "user": user,
            "rows": rows,
            "groups": groups,
            "group_id": group_id,
            "period": period,
            "periods": PERIODS,
            "my_place": my_place,
            "assignments": assignments,
            "error": request.query_params.get("err"),
        },
    )
