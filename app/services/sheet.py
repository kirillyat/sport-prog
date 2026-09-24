"""Ведомость группы: занятия и работы в колонках, студенты в строках.

За один семинар оценок бывает несколько: пришёл, поработал на паре, сдал
домашку. Поэтому единица ведомости — занятие, а колонки живут внутри него;
контрольная и прочее, что к семинару не привязано, стоит отдельными
колонками рядом.

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

from app.i18n import mark as N_
from app.models import (
    Assignment,
    Attendance,
    Group,
    GroupMembership,
    SheetColumn,
    SheetKind,
    SheetLesson,
    SheetMark,
    SheetScale,
    User,
    utcnow,
)
from app.services.progress import compute_progress

# Из чего обычно состоит семинар. Преподаватель снимает лишнее галочкой,
# но по умолчанию предлагаем то, ради чего ведомость и заводят.
PARTS: tuple[tuple[str, str, SheetKind], ...] = (
    ("attendance", N_("посещение"), SheetKind.attendance),
    ("classwork", N_("работа на семинаре"), SheetKind.manual),
    ("homework", N_("домашка"), SheetKind.manual),
)


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
    # Сколько баллов вообще можно набрать: сумма максимумов всех балльных
    # колонок, включая ещё не оценённые. Процент считается от курса целиком,
    # иначе первая же пятёрка из пяти покажет сто процентов за весь семестр.
    points_max: float = 0
    passed: int = 0
    gradable: int = 0
    # Работы, которых преподаватель ещё не касался. Без этого «2 из 7»
    # выглядит провалом там, где пять работ просто ждут проверки.
    ungraded: int = 0
    present: int = 0
    lessons: int = 0

    def _percent(self, part: float, whole: float) -> int | None:
        return round(part / whole * 100) if whole else None

    @property
    def points_percent(self) -> int | None:
        return self._percent(self.points, self.points_max)

    @property
    def passed_percent(self) -> int | None:
        return self._percent(self.passed, self.gradable)

    @property
    def present_percent(self) -> int | None:
        return self._percent(self.present, self.lessons)


@dataclass(slots=True)
class Block:
    """Занятие со своими колонками. Занятия нет — работа сама по себе."""

    lesson: SheetLesson | None
    columns: list[SheetColumn]

    @property
    def title(self) -> str:
        return self.lesson.title if self.lesson else ""


@dataclass(slots=True)
class Sheet:
    group: Group
    columns: list[SheetColumn]
    students: list[User]
    blocks: list[Block] = field(default_factory=list)
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
                # Занятие, которое ещё не отмечали, не пропуск: семестр
                # заводят вперёд, и «посещено 0 из 15» в сентябре — неправда.
                if value.attendance is None:
                    continue
                # Уважительная причина не пропуск, но и занятием не была:
                # в знаменателе она не участвует.
                if value.attendance == Attendance.excused:
                    continue
                total.lessons += 1
                if value.attendance == Attendance.present:
                    total.present += 1
                continue
            if column.scale == SheetScale.points:
                total.points += value.points or 0
                total.points_max += column.max_points
                continue
            total.gradable += 1
            if value.empty:
                total.ungraded += 1
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


async def lessons_of(session: AsyncSession, group_id: int) -> list[SheetLesson]:
    stmt = (
        select(SheetLesson)
        .where(SheetLesson.group_id == group_id)
        .order_by(SheetLesson.position, SheetLesson.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def columns_of(session: AsyncSession, group_id: int) -> list[SheetColumn]:
    """Колонки в порядке показа: сначала занятия по порядку, потом отдельные.

    Отдельные — в конце, потому что их в ведомости немного и они про итог
    курса: контрольная, проект, экзамен.
    """
    stmt = (
        select(SheetColumn)
        .where(SheetColumn.group_id == group_id)
        .order_by(SheetColumn.position, SheetColumn.id)
    )
    columns = list((await session.execute(stmt)).scalars().all())
    order = {lesson.id: index for index, lesson in enumerate(await lessons_of(session, group_id))}
    return sorted(
        columns,
        key=lambda c: (
            order.get(c.lesson_id, len(order)) if c.lesson_id else len(order) + 1,
            c.position,
            c.id,
        ),
    )


async def blocks_of(session: AsyncSession, group_id: int) -> list[Block]:
    columns = await columns_of(session, group_id)
    blocks: list[Block] = []
    for lesson in await lessons_of(session, group_id):
        inside = [c for c in columns if c.lesson_id == lesson.id]
        if inside:
            blocks.append(Block(lesson=lesson, columns=inside))
    alone = [c for c in columns if c.lesson_id is None]
    if alone:
        blocks.append(Block(lesson=None, columns=alone))
    return blocks


async def marks_of(session: AsyncSession, column_id: int) -> dict[int, SheetMark]:
    stmt = select(SheetMark).where(SheetMark.column_id == column_id)
    return {m.user_id: m for m in (await session.execute(stmt)).scalars().all()}


async def build(session: AsyncSession, group: Group) -> Sheet:
    """Собирает ведомость. Колонки-задания пересчитываются при каждом показе."""
    columns = await columns_of(session, group.id)
    students = await students_of(session, group)
    sheet = Sheet(
        group=group,
        columns=columns,
        students=students,
        blocks=await blocks_of(session, group.id),
    )

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


async def add_lesson(
    session: AsyncSession,
    group_id: int,
    title: str,
    held_on=None,
    parts: tuple[str, ...] = ("attendance", "classwork", "homework"),
) -> SheetLesson:
    """Заводит занятие вместе с его оценками — одной кнопкой.

    Пятнадцать семинаров по три колонки — это сорок пять форм, если заводить
    их по одной. Поэтому занятие создаётся целиком, а лишнее снимается
    галочкой заранее.
    """
    lesson = SheetLesson(
        group_id=group_id,
        title=title.strip(),
        held_on=held_on,
        position=len(await lessons_of(session, group_id)),
    )
    session.add(lesson)
    await session.flush()

    for index, (key, label, kind) in enumerate(PARTS):
        if key not in parts:
            continue
        session.add(
            SheetColumn(
                group_id=group_id,
                lesson_id=lesson.id,
                title=label,
                kind=kind,
                scale=SheetScale.pass_fail,
                position=index,
            )
        )
    return lesson


async def remove_lesson(session: AsyncSession, lesson: SheetLesson) -> None:
    for column in await columns_of(session, lesson.group_id):
        if column.lesson_id == lesson.id:
            await remove(session, column)
    await session.delete(lesson)


async def copy_columns(session: AsyncSession, source_id: int, target_id: int) -> int:
    """Переносит структуру ведомости в другую группу — без оценок.

    Колонку-задание переносим без привязки: задание выдано другой группе,
    и чужие результаты в ведомость попадать не должны.
    """
    taken = {lesson.title for lesson in await lessons_of(session, target_id)}
    alone = {c.title for c in await columns_of(session, target_id) if c.lesson_id is None}
    columns = await columns_of(session, source_id)
    added = 0

    for lesson in await lessons_of(session, source_id):
        if lesson.title in taken:
            continue
        copy = SheetLesson(
            group_id=target_id,
            title=lesson.title,
            # Дата чужая: у другой группы семинар в другой день.
            held_on=None,
            position=len(await lessons_of(session, target_id)),
        )
        session.add(copy)
        await session.flush()
        added += 1
        for column in (c for c in columns if c.lesson_id == lesson.id):
            session.add(_copy_column(column, target_id, copy.id))

    for column in (c for c in columns if c.lesson_id is None and c.title not in alone):
        session.add(_copy_column(column, target_id, None))
        added += 1
    return added


def _copy_column(column: SheetColumn, group_id: int, lesson_id: int | None) -> SheetColumn:
    return SheetColumn(
        group_id=group_id,
        lesson_id=lesson_id,
        title=column.title,
        kind=column.kind,
        scale=column.scale,
        max_points=column.max_points,
        position=column.position,
        # Задание выдано другой группе — чужие результаты сюда попадать
        # не должны, поэтому колонка переносится без привязки.
        assignment_id=None,
        required_solved=column.required_solved,
    )


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
