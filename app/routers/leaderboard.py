from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Request
from sqlalchemy import select

from app.deps import CurrentUser, OptionalInt, SessionDep
from app.models import Group, utcnow
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
    group_id: OptionalInt = None,
    period: str = "all",
):
    if period not in PERIODS:
        period = "all"
    _, days = PERIODS[period]
    since = utcnow() - timedelta(days=days) if days else None

    if user.is_teacher:
        stmt = select(Group).where(Group.is_archived.is_(False)).order_by(Group.title)
        groups = list((await session.execute(stmt)).scalars().all())
    else:
        groups = await groups_for_user(session, user)

    # Общего табло нет: сравнивать студентов из разных групп не за что —
    # задания у них разные. Не выбрана группа или выбрана чужая — берём первую свою.
    if group_id is None or all(g.id != group_id for g in groups):
        group_id = groups[0].id if groups else None

    rows = await build_leaderboard(session, group_id=group_id, since=since) if group_id else []
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
            "error": request.query_params.get("err"),
        },
    )
