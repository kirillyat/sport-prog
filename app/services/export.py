"""Выгрузка в CSV.

Разделитель — точка с запятой, кодировка с BOM: так файл открывается двойным
щелчком в Excel с русской локалью и не рассыпается в один столбец. Google Sheets
и pandas такой файл тоже читают.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.i18n import mark as N_
from app.i18n import translate as _
from app.models import Assignment, Group, GroupMembership, SolveStatus
from app.services.leaderboard import LeaderboardRow
from app.services.progress import AssignmentProgress
from app.templating import fmt_dt, fmt_points

DELIMITER = ";"
ENCODING = "utf-8-sig"

# Значения переводятся при выгрузке, а не здесь: словарь собирается один раз
# на импорте, а язык известен только внутри запроса.
CELL = {
    SolveStatus.solved_in_time: N_("в срок"),
    SolveStatus.solved_late: N_("после дедлайна"),
    SolveStatus.solved_before: N_("до выдачи"),
    SolveStatus.not_solved: "",
}


def to_csv(header: list[str], rows: list[list[object]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=DELIMITER, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode(ENCODING)


def _moment(value: datetime | None) -> str:
    return fmt_dt(value) if value else ""


async def _groups_of(session: AsyncSession, user_ids: list[int]) -> dict[int, list[str]]:
    if not user_ids:
        return {}
    stmt = (
        select(GroupMembership.user_id, Group.title)
        .join(Group, Group.id == GroupMembership.group_id)
        .where(GroupMembership.user_id.in_(user_ids))
        .order_by(Group.title)
    )
    out: dict[int, list[str]] = {}
    for user_id, title in (await session.execute(stmt)).all():
        out.setdefault(user_id, []).append(title)
    return out


async def leaderboard_csv(session: AsyncSession, rows: list[LeaderboardRow]) -> bytes:
    groups = await _groups_of(session, [row.user.id for row in rows])
    header = [
        _("Место"), _("Студент"), _("Группы"), _("Зачтено"), _("Выдано"), _("Доля, %"),
        _("После дедлайна"), _("Бонусы"), _("Последнее решение"), _("Подтверждён"),
    ]
    body: list[list[object]] = []
    for row in rows:
        body.append([
            row.place,
            row.user.display_name,
            ", ".join(groups.get(row.user.id, [])),
            row.solved,
            row.assigned,
            row.share,
            row.late,
            fmt_points(row.bonus),
            _moment(row.last_solved_at),
            _("да") if row.user.oidc_sub else _("нет"),
        ])
    return to_csv(header, body)


def assignment_csv(assignment: Assignment, progress: AssignmentProgress) -> bytes:
    header = [_("Студент"), _("Зачтено"), _("Всего задач")]
    header += [f"{index}. {problem.title}" for index, problem in enumerate(progress.problems, 1)]

    body: list[list[object]] = []
    for student in sorted(progress.participants, key=lambda u: u.display_name):
        line: list[object] = [
            student.display_name,
            progress.solved_count(student.id),
            progress.total_problems,
        ]
        line += [_(CELL[progress.cell(student.id, p.id).status]) for p in progress.problems]
        body.append(line)
    return to_csv(header, body)


def sheet_csv(built) -> bytes:
    """Ведомость группы. Итог тремя числами — так же, как на странице."""
    header = [_("Студент")]
    header += [column.title for column in built.columns]
    header += [_("Баллы"), _("Зачтено"), _("Посещено")]

    body: list[list[object]] = []
    for student in built.students:
        total = built.total(student.id)
        line: list[object] = [student.display_name]
        for column in built.columns:
            line.append(_cell(built.value(student.id, column.id), column))
        line += [
            total.points,
            f"{total.passed} / {total.gradable}",
            f"{total.present} / {total.lessons}",
        ]
        body.append(line)
    return to_csv(header, body)


def _cell(value, column) -> str:
    from app.models import Attendance, SheetKind, SheetScale

    if column.kind == SheetKind.attendance:
        return {
            Attendance.present: _("был"),
            Attendance.absent: _("не был"),
            Attendance.excused: _("уважительная"),
        }.get(value.attendance, "")
    if column.kind == SheetKind.assignment:
        return "" if value.total is None else f"{value.solved} / {value.total}"
    if column.scale == SheetScale.points:
        return "" if value.points is None else str(value.points)
    if value.passed is None:
        return ""
    return _("зачёт") if value.passed else _("незачёт")


def filename(prefix: str, moment: datetime) -> str:
    """Только латиница и цифры: имя файла едет в заголовок ответа."""
    return f"{prefix}-{moment:%Y-%m-%d}.csv"


def response_headers(name: str) -> dict[str, str]:
    return {"Content-Disposition": f'attachment; filename="{name}"'}
