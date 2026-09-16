"""Решения, присланные студентами файлом.

Зачем вообще: у LeetCode исходник посылки видно только автору, официального
способа получить его у портала нет и не будет. Поэтому там, где преподавателю
нужен текст решения, задание требует прислать его отдельно.

Файлы лежат на диске рядом с базой, имя на диске случайное — пользовательское
в путь не попадает, как и у материалов.
"""

from __future__ import annotations

import secrets
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import ReviewStatus, SolutionUpload, utcnow

MAX_BYTES = 2 * 1024 * 1024

# Исходники и разборы. Архивы и бинарники не принимаем: проверять их всё равно
# нельзя, а хранить чужой исполняемый файл на портале незачем.
ALLOWED = {
    ".py": "text/x-python",
    ".ipynb": "application/x-ipynb+json",
    ".cpp": "text/x-c++src",
    ".cc": "text/x-c++src",
    ".c": "text/x-csrc",
    ".h": "text/x-chdr",
    ".java": "text/x-java-source",
    ".kt": "text/x-kotlin",
    ".go": "text/x-go",
    ".rs": "text/rust",
    ".js": "text/javascript",
    ".ts": "text/x-typescript",
    ".cs": "text/x-csharp",
    ".rb": "text/x-ruby",
    ".txt": "text/plain",
    ".md": "text/markdown",
}

EXTENSIONS_HINT = "py, ipynb, cpp, c, java, kt, go, rs, js, ts, cs, rb, txt, md"


def storage_dir() -> Path:
    path = settings.data_dir / "solutions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def extension_of(filename: str) -> str:
    return Path(filename or "").suffix.lower()


def is_allowed(filename: str) -> bool:
    return extension_of(filename) in ALLOWED


def content_type_for(filename: str) -> str:
    return ALLOWED.get(extension_of(filename), "text/plain")


def safe_filename(filename: str) -> str:
    name = Path(filename or "").name.strip()
    return name[:200] or "solution.txt"


def new_stored_name(filename: str) -> str:
    return secrets.token_hex(16) + extension_of(filename)


def path_for(stored_name: str) -> Path:
    return storage_dir() / Path(stored_name).name


def save(stored_name: str, data: bytes) -> None:
    path_for(stored_name).write_bytes(data)


def remove(stored_name: str) -> None:
    path_for(stored_name).unlink(missing_ok=True)


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
    filename: str,
    data: bytes,
) -> SolutionUpload:
    """Кладём решение. Повторная отправка заменяет файл и снова просит проверки."""
    existing = await get(session, assignment_id, problem_id, user_id)
    stored = new_stored_name(filename)
    save(stored, data)

    if existing is not None:
        remove(existing.stored_name)
        existing.filename = safe_filename(filename)
        existing.stored_name = stored
        existing.size = len(data)
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
        filename=safe_filename(filename),
        stored_name=stored,
        size=len(data),
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


async def pending(session: AsyncSession, limit: int = 100) -> list[SolutionUpload]:
    """Очередь проверки: сначала то, что прислали раньше."""
    stmt = (
        select(SolutionUpload)
        .where(SolutionUpload.status == ReviewStatus.pending)
        .order_by(SolutionUpload.submitted_at)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())
