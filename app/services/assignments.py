"""Выдача задания: проверки, по заданию на группу, рассылка.

Здесь, а не в обработчике формы, потому что выдать задание можно и из
нейросети по MCP — и правила у обоих путей должны быть одни.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import notify
from app.i18n import translate as _
from app.models import Assignment, Group, ProblemSet, ProblemSetItem, User, utcnow


class AssignmentError(ValueError):
    """Задание не выдать: сообщение уже переведено и годится для человека."""


async def issue(
    session: AsyncSession,
    author: User,
    *,
    title: str,
    problem_set_id: int,
    group_ids: list[int],
    description: str = "",
    starts_at: datetime | None = None,
    deadline: datetime | None = None,
    count_prior_solves: bool = False,
    requires_solution: bool = False,
) -> tuple[list[Assignment], int]:
    """Выданные задания и сколько сообщений ушло в Telegram.

    Начало в будущем — студенты не видят задание до начала, и сообщение в
    Telegram тоже уйдёт тогда (`notify.announce_started`), а сейчас — ноль.

    Несколько групп — несколько заданий, по одному на группу, а не одно на
    всех. Табло, напоминания и матрица живут по группам, и у каждой группы
    своё задание можно продлить или удалить, не задевая соседей.
    """
    title = title.strip()
    if not title:
        raise AssignmentError(_("Пустое название"))
    if await session.get(ProblemSet, problem_set_id) is None:
        raise AssignmentError(_("Список задач не найден"))
    # dict.fromkeys убирает повторы и сохраняет порядок галочек.
    group_ids = list(dict.fromkeys(group_ids))
    for group_id in group_ids:
        if await session.get(Group, group_id) is None:
            raise AssignmentError(_("Группа не найдена"))

    start = starts_at or utcnow()
    if deadline is not None and deadline <= start:
        raise AssignmentError(_("Дедлайн раньше начала"))

    # Ни одной группы — задание всем, как и раньше при пустом «Кому».
    created = [
        Assignment(
            title=title,
            description=description.strip() or None,
            problem_set_id=problem_set_id,
            group_id=group_id,
            assigned_at=start,
            deadline=deadline,
            created_by_id=author.id,
            count_prior_solves=count_prior_solves,
            requires_solution=requires_solution,
            # Начнётся позже — объявим в момент начала, не раньше.
            announced=start <= utcnow(),
        )
        for group_id in (group_ids or [None])
    ]
    session.add_all(created)
    await session.commit()
    for assignment in created:
        await session.refresh(assignment)
    # После refresh связи не загружены — считаем задачи отдельным запросом.
    problems = await session.scalar(
        select(func.count()).select_from(ProblemSetItem).where(
            ProblemSetItem.problem_set_id == problem_set_id
        )
    )
    delivered = 0
    for assignment in created:
        if assignment.announced:
            delivered += await notify.notify_assignment(assignment, int(problems or 0), session)
    return created, delivered


async def edit(
    session: AsyncSession, assignment: Assignment, *, title: str, description: str
) -> None:
    """Правится только то, что не меняет подсчёт: название и описание.

    Список задач, адресат и баллы задаются при создании — иначе рейтинг
    задним числом менялся бы у всех участников. Срок двигается отдельно
    и только вперёд (`deadlines.extend_deadline`).
    """
    title = title.strip()
    if not title:
        raise AssignmentError(_("Пустое название"))
    assignment.title = title
    assignment.description = description.strip() or None
    await session.commit()
