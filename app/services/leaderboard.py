"""Табло группы.

Одна метрика: сколько задач из выданных человек закрыл до дедлайна. Задачи
назначает преподаватель, и у всех в группе они одни и те же — поэтому
взвешивать их по сложности незачем: нафармить лёгких всё равно нельзя.

Равный счёт разводит тот, кто раньше закончил. Ручные бонусы жюри на место
не влияют — они показываются отдельно, чтобы итог оставался проверяемым.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.access import confirmed_clause
from app.models import (
    Assignment,
    BonusPoint,
    GroupMembership,
    Role,
    SolveStatus,
    User,
    utcnow,
)
from app.services.progress import compute_progress

# За какой срок считается движение в таблице.
MOVEMENT_WINDOW = timedelta(days=7)
# Кто не решил ничего, сортируется последним.
NEVER = datetime.max.replace(tzinfo=UTC)


@dataclass(slots=True)
class LeaderboardRow:
    user: User
    assigned: int = 0
    late: int = 0
    bonus: float = 0.0
    # Моменты засчитанных решений — из них считаются и счёт, и порядок, и движение.
    solve_times: list[datetime] = field(default_factory=list)
    place: int = 0
    movement: int | None = None

    @property
    def solved(self) -> int:
        return len(self.solve_times)

    @property
    def last_solved_at(self) -> datetime | None:
        return max(self.solve_times) if self.solve_times else None

    @property
    def share(self) -> float:
        """Доля выданного, ушедшая в зачёт — для полосы прогресса."""
        return round(100 * self.solved / self.assigned) if self.assigned else 0

    def solved_by(self, moment: datetime) -> int:
        return sum(1 for t in self.solve_times if t <= moment)

    def finished_by(self, moment: datetime) -> datetime | None:
        done = [t for t in self.solve_times if t <= moment]
        return max(done) if done else None


def _order(row: LeaderboardRow, moment: datetime | None = None) -> tuple:
    """Больше решено — выше; при равенстве выше тот, кто закончил раньше."""
    solved = row.solved if moment is None else row.solved_by(moment)
    last = row.last_solved_at if moment is None else row.finished_by(moment)
    return (-solved, last or NEVER, row.user.display_name)


async def _group_member_ids(session: AsyncSession, group_id: int) -> set[int]:
    rows = await session.execute(
        select(GroupMembership.user_id).where(GroupMembership.group_id == group_id)
    )
    return set(rows.scalars().all())


async def _users_in_scope(session: AsyncSession, group_id: int | None) -> list[User]:
    """Преподаватели вне рейтинга: они выдают задания, а не соревнуются.

    Неподтверждённые тоже: пока вуз не подтверждён, человек не участник.
    """
    stmt = select(User).where(
        User.is_active.is_(True), User.role != Role.teacher, confirmed_clause()
    )
    if group_id is not None:
        stmt = stmt.join(GroupMembership, GroupMembership.user_id == User.id).where(
            GroupMembership.group_id == group_id
        )
    stmt = stmt.order_by(User.display_name)
    return list((await session.execute(stmt)).scalars().all())


async def _assignments_in_scope(
    session: AsyncSession, group_id: int | None, since: datetime | None
) -> list[Assignment]:
    stmt = select(Assignment)
    if group_id is not None:
        # Клубные задания (обе ссылки пусты) относятся и к этой группе тоже.
        stmt = stmt.where(
            (Assignment.group_id == group_id)
            | ((Assignment.group_id.is_(None)) & (Assignment.user_id.is_(None)))
        )
    if since is not None:
        stmt = stmt.where(Assignment.assigned_at >= since)
    return list((await session.execute(stmt)).scalars().all())


def _set_places(rows: list[LeaderboardRow]) -> list[LeaderboardRow]:
    """Места и движение за неделю. Движения нет, пока нет недельной истории."""
    rows.sort(key=_order)
    for index, row in enumerate(rows, start=1):
        row.place = index

    week_ago = utcnow() - MOVEMENT_WINDOW
    if not any(row.solved_by(week_ago) for row in rows):
        return rows

    was = sorted(rows, key=lambda r: _order(r, week_ago))
    old_place = {row.user.id: index for index, row in enumerate(was, start=1)}
    for row in rows:
        row.movement = old_place[row.user.id] - row.place
    return rows


async def build_leaderboard(
    session: AsyncSession,
    *,
    group_id: int | None = None,
    since: datetime | None = None,
) -> list[LeaderboardRow]:
    users = await _users_in_scope(session, group_id)
    rows = {u.id: LeaderboardRow(user=u) for u in users}
    if not rows:
        return []

    for assignment in await _assignments_in_scope(session, group_id, since):
        participants = list(users)
        if assignment.user_id is not None:
            participants = [u for u in participants if u.id == assignment.user_id]
        elif assignment.group_id is not None:
            members = await _group_member_ids(session, assignment.group_id)
            participants = [u for u in participants if u.id in members]
        if not participants:
            continue

        progress = await compute_progress(session, assignment, participants)
        for user in participants:
            row = rows[user.id]
            row.assigned += progress.total_problems
            for cell in progress.row(user.id):
                # В зачёт идёт только решённое до дедлайна.
                # Опоздания считаем отдельно и показываем значком.
                if cell.status == SolveStatus.solved_in_time and cell.solved_at is not None:
                    row.solve_times.append(cell.solved_at)
                elif cell.status == SolveStatus.solved_late:
                    row.late += 1

    bonus_stmt = select(BonusPoint.user_id, func.sum(BonusPoint.points)).where(
        BonusPoint.user_id.in_(list(rows))
    )
    if since is not None:
        bonus_stmt = bonus_stmt.where(BonusPoint.granted_at >= since)
    for user_id, total in (await session.execute(bonus_stmt.group_by(BonusPoint.user_id))).all():
        if user_id in rows:
            rows[user_id].bonus = round(float(total or 0), 2)

    return _set_places(list(rows.values()))
