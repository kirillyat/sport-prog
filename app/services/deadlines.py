"""Перенос дедлайна уже выданного задания.

Зачёт не хранится, а считается из посылок и дедлайна (`Cell.counts`), поэтому
перенос срока сразу пересчитывает всё: решённое между старым и новым сроком
становится «в срок» на табло, в матрице и в выгрузке одновременно.
"""

from __future__ import annotations

from datetime import datetime

from app.i18n import translate as _
from app.models import Assignment, utcnow


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
