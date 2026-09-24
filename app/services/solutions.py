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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ReviewStatus, SolutionUpload, utcnow

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
) -> None:
    upload.status = status
    upload.comment = comment.strip()[:500] or None
    upload.reviewed_by_id = reviewer_id
    upload.reviewed_at = utcnow()
    await session.commit()


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
