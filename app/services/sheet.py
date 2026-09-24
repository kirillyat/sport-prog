"""Ведомость группы: работы в колонках, студенты в строках.

Колонка бывает трёх видов. Задание считается само — тем же правилом
`Cell.counts`, что табло и матрица, и ничего не хранит: иначе у портала
появились бы две правды об одном и том же, посчитанная и вписанная.
Посещаемость и ручные оценки, наоборот, только из рук преподавателя.

Итог строки — три разных числа, а не одно: сумма баллов, сколько зачётов
из скольких и сколько занятий посещено. Смешивать их портал не берётся:
вес каждой части — дело курса, и меняется он чаще, чем код.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Assignment,
    Attendance,
    Group,
    GroupMembership,
    SheetColumn,
    SheetKind,
    SheetMark,
    SheetScale,
    User,
    utcnow,
)
from app.services.progress import compute_progress


@dataclass(slots=True)
class Value:
    """Что стоит в ячейке. Пустая — работа ещё не оценена."""

    solved: int | None = None
    total: int | None = None
    points: float | None = None
    passed: bool | None = None
    attendance: Attendance | None = None
    comment: str = ""

    @property
    def empty(self) -> bool:
        return (
            self.points is None
            and self.passed is None
            and self.attendance is None
            and self.solved is None
        )

    @property
    def counted(self) -> bool:
        """Идёт ли работа в зачёт. Для баллов зачёта нет — там сумма."""
        return bool(self.passed)


@dataclass(slots=True)
class Total:
    points: float = 0
    passed: int = 0
    gradable: int = 0
    present: int = 0
    lessons: int = 0


@dataclass(slots=True)
class Sheet:
    group: Group
    columns: list[SheetColumn]
    students: list[User]
    values: dict[tuple[int, int], Value] = field(default_factory=dict)

    def value(self, user_id: int, column_id: int) -> Value:
        return self.values.get((user_id, column_id), Value())

    def row(self, user_id: int) -> list[Value]:
        return [self.value(user_id, c.id) for c in self.columns]

    def total(self, user_id: int) -> Total:
        total = Total()
        for column in self.columns:
            value = self.value(user_id, column.id)
            if column.kind == SheetKind.attendance:
                total.lessons += 1
                # Уважительная причина не считается пропуском, но и занятием
                # не была: в знаменателе она не участвует.
                if value.attendance == Attendance.excused:
                    total.lessons -= 1
                elif value.attendance == Attendance.present:
                    total.present += 1
                continue
            if column.scale == SheetScale.points:
                total.points += value.points or 0
                continue
            total.gradable += 1
            if value.counted:
                total.passed += 1
        return total


async def students_of(session: AsyncSession, group: Group) -> list[User]:
    stmt = (
        select(User)
        .join(GroupMembership, GroupMembership.user_id == User.id)
        .where(GroupMembership.group_id == group.id, User.is_active.is_(True))
        .order_by(func.lower(User.display_name))
    )
    return list((await session.execute(stmt)).scalars().all())


async def columns_of(session: AsyncSession, group_id: int) -> list[SheetColumn]:
    stmt = (
        select(SheetColumn)
        .where(SheetColumn.group_id == group_id)
        .order_by(SheetColumn.position, SheetColumn.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def marks_of(session: AsyncSession, column_id: int) -> dict[int, SheetMark]:
    stmt = select(SheetMark).where(SheetMark.column_id == column_id)
    return {m.user_id: m for m in (await session.execute(stmt)).scalars().all()}


async def build(session: AsyncSession, group: Group) -> Sheet:
    """Собирает ведомость. Колонки-задания пересчитываются при каждом показе."""
    columns = await columns_of(session, group.id)
    students = await students_of(session, group)
    sheet = Sheet(group=group, columns=columns, students=students)

    for column in columns:
        if column.kind == SheetKind.assignment:
            await _fill_from_assignment(session, sheet, column)
            continue
        for user_id, mark in (await marks_of(session, column.id)).items():
            sheet.values[(user_id, column.id)] = Value(
                points=mark.points,
                passed=mark.passed,
                attendance=mark.attendance,
                comment=mark.comment or "",
            )
    return sheet


async def _fill_from_assignment(
    session: AsyncSession, sheet: Sheet, column: SheetColumn
) -> None:
    assignment = (
        await session.get(Assignment, column.assignment_id)
        if column.assignment_id is not None
        else None
    )
    if assignment is None:
        return
    progress = await compute_progress(session, assignment)
    total = progress.total_problems
    # Порог не задан — нужны все задачи задания.
    need = column.required_solved or total
    for student in sheet.students:
        solved = progress.solved_count(student.id)
        sheet.values[(student.id, column.id)] = Value(
            solved=solved,
            total=total,
            passed=total > 0 and solved >= need,
            points=solved if column.scale == SheetScale.points else None,
        )


async def put(
    session: AsyncSession,
    column: SheetColumn,
    user_id: int,
    *,
    points: float | None = None,
    passed: bool | None = None,
    attendance: Attendance | None = None,
    comment: str | None = None,
    graded_by_id: int | None = None,
) -> None:
    """Ставит оценку. Пустая оценка без комментария — это снятие отметки."""
    mark = await session.scalar(
        select(SheetMark).where(SheetMark.column_id == column.id, SheetMark.user_id == user_id)
    )
    empty = points is None and passed is None and attendance is None and not comment
    if mark is None and empty:
        return
    if mark is not None and empty:
        await session.delete(mark)
        return
    if mark is None:
        mark = SheetMark(column_id=column.id, user_id=user_id)
        session.add(mark)
    mark.points = points
    mark.passed = passed
    mark.attendance = attendance
    mark.comment = (comment or "").strip() or None
    mark.graded_by_id = graded_by_id
    mark.graded_at = utcnow()


async def copy_columns(session: AsyncSession, source_id: int, target_id: int) -> int:
    """Переносит структуру ведомости в другую группу — без оценок.

    Колонку-задание переносим без привязки: задание выдано другой группе,
    и чужие результаты в ведомость попадать не должны.
    """
    existing = {c.title for c in await columns_of(session, target_id)}
    shift = await session.scalar(
        select(func.coalesce(func.max(SheetColumn.position), -1)).where(
            SheetColumn.group_id == target_id
        )
    )
    added = 0
    for column in await columns_of(session, source_id):
        if column.title in existing:
            continue
        added += 1
        session.add(
            SheetColumn(
                group_id=target_id,
                title=column.title,
                kind=column.kind,
                scale=column.scale,
                max_points=column.max_points,
                position=int(shift or 0) + added,
                assignment_id=None,
                required_solved=column.required_solved,
                held_on=column.held_on,
            )
        )
    return added


async def remove(session: AsyncSession, column: SheetColumn) -> None:
    await session.execute(delete(SheetMark).where(SheetMark.column_id == column.id))
    await session.delete(column)


async def my_rows(session: AsyncSession, user: User) -> list[tuple[Group, Sheet]]:
    """Ведомости всех групп студента — для его собственной страницы оценок."""
    stmt = (
        select(Group)
        .join(GroupMembership, GroupMembership.group_id == Group.id)
        .where(GroupMembership.user_id == user.id, Group.is_archived.is_(False))
        .order_by(Group.title)
    )
    groups = list((await session.execute(stmt)).scalars().all())
    return [(group, await build(session, group)) for group in groups]
