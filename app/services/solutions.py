"""Код решения, присланный студентом.

Зачем вообще: у LeetCode исходник посылки видно только автору, официального
способа получить его у портала нет и не будет. Поэтому там, где преподавателю
нужен текст решения, студент присылает его сам.

Код хранится текстом в базе. Файлов не принимаем: решение — это несколько
десятков строк, ради них незачем заводить загрузку вложений, следить за
расширениями и подчищать файлы после удаления задания.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.i18n import translate as _
from app.models import Assignment, ReviewStatus, SolutionUpload, Submission, utcnow

# Потолок на длину: решение задачи столько не занимает, а вставленный по
# ошибке ноутбук или лог — запросто.
MAX_CHARS = 60_000

# Подсветка и имя при скачивании. Курс питоновский; другой язык всё равно
# сохранится как есть, просто подсветится по питоновским правилам.
FILENAME = "solution.py"


async def for_assignment(
    session: AsyncSession, assignment_id: int, user_ids: list[int] | None = None
) -> dict[tuple[int, int], SolutionUpload]:
    """(user_id, problem_id) -> решение. Ключ тот же, что у клеток прогресса."""
    stmt = select(SolutionUpload).where(SolutionUpload.assignment_id == assignment_id)
    if user_ids is not None:
        if not user_ids:
            return {}
        stmt = stmt.where(SolutionUpload.user_id.in_(user_ids))
    rows = (await session.execute(stmt)).scalars().all()
    return {(row.user_id, row.problem_id): row for row in rows}


async def get(
    session: AsyncSession, assignment_id: int, problem_id: int, user_id: int
) -> SolutionUpload | None:
    return await session.scalar(
        select(SolutionUpload).where(
            SolutionUpload.assignment_id == assignment_id,
            SolutionUpload.problem_id == problem_id,
            SolutionUpload.user_id == user_id,
        )
    )


async def put(
    session: AsyncSession,
    *,
    assignment_id: int,
    problem_id: int,
    user_id: int,
    code: str,
) -> SolutionUpload:
    """Кладём код. Повторная отправка заменяет его и снова просит проверки."""
    existing = await get(session, assignment_id, problem_id, user_id)
    if existing is not None:
        existing.code = code
        existing.submitted_at = utcnow()
        existing.status = ReviewStatus.pending
        existing.comment = None
        existing.reviewed_by_id = None
        existing.reviewed_at = None
        await session.commit()
        return existing

    upload = SolutionUpload(
        assignment_id=assignment_id,
        problem_id=problem_id,
        user_id=user_id,
        code=code,
    )
    session.add(upload)
    await session.commit()
    return upload


async def review(
    session: AsyncSession,
    upload: SolutionUpload,
    *,
    status: ReviewStatus,
    comment: str,
    reviewer_id: int,
) -> str | None:
    """Решение проверяющего. Возвращает текст отказа или None.

    Отклонение снимает зачёт, поэтому без объяснения его не принимаем:
    студент должен понять, что исправлять, а не гадать.
    """
    if status == ReviewStatus.rejected and not comment.strip():
        return _("Отклонять без объяснения нельзя")
    upload.status = status
    upload.comment = comment.strip()[:500] or None
    upload.reviewed_by_id = reviewer_id
    upload.reviewed_at = utcnow()
    await session.commit()
    return None


@dataclass(slots=True)
class Tally:
    """Сколько решений студент прислал по заданию и что с ними стало."""

    total: int = 0          # задач в задании
    sent: int = 0
    waiting: int = 0
    rejected: int = 0

    @property
    def missing(self) -> int:
        return self.total - self.sent

    @property
    def complete(self) -> bool:
        return self.missing == 0 and self.rejected == 0


def tally(
    uploads: dict[tuple[int, int], SolutionUpload], user_id: int, problem_ids: list[int]
) -> Tally:
    out = Tally(total=len(problem_ids))
    for problem_id in problem_ids:
        upload = uploads.get((user_id, problem_id))
        if upload is None:
            continue
        out.sent += 1
        if upload.status == ReviewStatus.pending:
            out.waiting += 1
        elif upload.status == ReviewStatus.rejected:
            out.rejected += 1
    return out


async def pending_count(session: AsyncSession) -> int:
    from sqlalchemy import func

    stmt = (
        select(func.count())
        .select_from(SolutionUpload)
        .where(SolutionUpload.status == ReviewStatus.pending)
    )
    return int((await session.execute(stmt)).scalar_one())


async def next_pending(session: AsyncSession, besides: int | None = None) -> SolutionUpload | None:
    """Следующее в очереди. Нужно, чтобы после решения открывать его сразу:
    возвращаться в список ради одного клика — половина работы проверяющего."""
    stmt = (
        select(SolutionUpload)
        .where(SolutionUpload.status == ReviewStatus.pending)
        .order_by(SolutionUpload.submitted_at)
    )
    if besides is not None:
        stmt = stmt.where(SolutionUpload.id != besides)
    return await session.scalar(stmt.limit(1))


async def pending(session: AsyncSession, limit: int = 100) -> list[SolutionUpload]:
    """Очередь проверки: сначала то, что прислали раньше."""
    stmt = (
        select(SolutionUpload)
        .where(SolutionUpload.status == ReviewStatus.pending)
        .order_by(SolutionUpload.submitted_at)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


@dataclass(slots=True)
class Picked:
    """Решение студента по задаче задания — то, которое читают проверяющий,
    поиск похожих решений и нейросеть по MCP. Одно правило на всех: иначе
    страница сравнивала бы одно решение, а нейросеть показывала другое."""

    source: str                 # "upload" — прислан к задаче, "submission" — посылка
    code: str
    submitted_at: datetime
    language: str | None
    accepted: bool | None       # у присланного кода вердикта нет
    review: str | None          # а у посылки — проверки
    attempts: int
    url: str                    # где его прочитать на портале


async def picked_for_assignment(
    session: AsyncSession,
    assignment: Assignment,
    user_ids: list[int],
    problem_ids: list[int],
) -> dict[tuple[int, int], Picked]:
    """(user_id, problem_id) -> решение, у кого оно есть.

    Присланный к задаче код важнее посылки: его студент выбрал сам. Без него —
    посылка после выдачи задания с исходником: последняя принятая, а если
    принятой нет — последняя. `attempts` — все посылки после выдачи.
    """
    if not user_ids or not problem_ids:
        return {}
    uploads = await for_assignment(session, assignment.id, user_ids)

    latest: dict[tuple[int, int], Submission] = {}
    attempts: dict[tuple[int, int], int] = {}
    rows = await session.execute(
        select(Submission)
        .where(
            Submission.user_id.in_(user_ids),
            Submission.problem_id.in_(problem_ids),
            Submission.submitted_at >= assignment.assigned_at,
        )
        .order_by(Submission.submitted_at)
    )
    for sub in rows.scalars():
        key = (sub.user_id, sub.problem_id)
        attempts[key] = attempts.get(key, 0) + 1
        if not sub.code:
            continue
        current = latest.get(key)
        # Принятое важнее непринятого, среди равных — более позднее.
        if current is None or sub.is_accepted or not current.is_accepted:
            latest[key] = sub

    out: dict[tuple[int, int], Picked] = {}
    for user_id in user_ids:
        for problem_id in problem_ids:
            key = (user_id, problem_id)
            upload, sub = uploads.get(key), latest.get(key)
            if upload is not None:
                out[key] = Picked(
                    source="upload", code=upload.code or "", submitted_at=upload.submitted_at,
                    language=None, accepted=None, review=upload.status.value,
                    attempts=attempts.get(key, 0), url=f"/solutions/{upload.id}",
                )
            elif sub is not None:
                out[key] = Picked(
                    source="submission", code=sub.code or "", submitted_at=sub.submitted_at,
                    language=sub.language, accepted=sub.is_accepted, review=None,
                    attempts=attempts.get(key, 0), url=f"/submissions/{sub.id}",
                )
    return out
