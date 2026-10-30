"""Перенос дедлайна уже выданного задания.

Зачёт не хранится, а считается из посылок и дедлайна (`Cell.counts`), поэтому
перенос срока сразу пересчитывает всё: решённое между старым и новым сроком
становится «в срок» на табло, в матрице и в выгрузке одновременно.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.i18n import translate as _
from app.models import Assignment, utcnow
from app.services.progress import compute_progress, participants_for_assignment


def extend_deadline(assignment: Assignment, new_deadline: datetime | None) -> str | None:
    """Переносит срок вперёд или снимает его. Возвращает текст ошибки или None.

    Только вперёд: ранний срок задним числом снял бы зачёт с тех, кто решил
    честно в прежний срок. Задание без дедлайна сдвигать некуда — поставить
    ему срок значит ужесточить правила после выдачи.
    """
    current = assignment.deadline
    if current is None:
        return _("У задания нет дедлайна — сдвигать нечего")
    if new_deadline is not None:
        if new_deadline <= current:
            return _("Дедлайн можно только отодвинуть")
        if new_deadline <= assignment.assigned_at:
            return _("Дедлайн раньше начала")
    assignment.deadline = new_deadline
    # Напоминание относилось к старому сроку. Если новый ещё впереди — пусть
    # бот напомнит о нём заново, иначе студент запомнит прежнюю дату.
    if new_deadline is None or new_deadline > utcnow():
        assignment.reminded_at = None
    return None


@dataclass(slots=True)
class Shift:
    """Что перенос сделал бы с одним заданием: старый срок и сколько зачётов было и стало."""

    assignment_id: int
    assignment: Assignment | None
    old_deadline: datetime | None = None
    credits_before: int = 0
    credits_after: int = 0
    error: str | None = None


async def _credits(session: AsyncSession, assignment: Assignment) -> int:
    # Преподаватель прорешивает задания заранее — его решения в зачёт не идут.
    students = [
        user for user in await participants_for_assignment(session, assignment)
        if not user.is_teacher
    ]
    progress = await compute_progress(session, assignment, students)
    return sum(progress.solved_count(user.id) for user in students)


async def extend_many(
    session: AsyncSession, assignment_ids: list[int], new_deadline: datetime | None
) -> list[Shift]:
    """Переносит срок у нескольких заданий в сессии, не записывая.

    Записать или откатить решает вызывающий: `commit`, только если ни у
    одного задания нет `error`. Половина продлённых заданий хуже, чем ни
    одного: потом не вспомнить, какие уже сдвинуты.
    """
    shifts = []
    # dict.fromkeys убирает повторы: дважды «продлённое» задание во втором
    # проходе отказало бы «можно только отодвинуть».
    for assignment_id in dict.fromkeys(assignment_ids):
        assignment = await session.get(Assignment, assignment_id)
        if assignment is None:
            shifts.append(Shift(assignment_id, None, error=_("Задание не найдено")))
            continue
        shift = Shift(
            assignment_id,
            assignment,
            old_deadline=assignment.deadline,
            credits_before=await _credits(session, assignment),
        )
        shift.error = extend_deadline(assignment, new_deadline)
        shift.credits_after = (
            shift.credits_before if shift.error else await _credits(session, assignment)
        )
        shifts.append(shift)
    return shifts
