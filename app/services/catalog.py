from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Platform, Problem, SyncState, utcnow
from app.platforms import CodeforcesClient, LeetCodeClient
from app.platforms.base import RemoteProblem

logger = logging.getLogger(__name__)

CHUNK = 400


async def _upsert_problems(
    session: AsyncSession, platform: Platform, problems: list[RemoteProblem]
) -> int:
    written = 0
    for start in range(0, len(problems), CHUNK):
        rows = [
            {
                "platform": platform.value,
                "external_id": p.external_id,
                "slug": p.slug,
                "title": p.title,
                "url": p.url,
                "difficulty": p.difficulty,
                "rating": p.rating,
                "tags": p.tags,
                "is_paid_only": p.is_paid_only,
                "updated_at": utcnow(),
            }
            for p in problems[start : start + CHUNK]
        ]
        if not rows:
            continue
        stmt = sqlite_insert(Problem).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Problem.platform, Problem.external_id],
            set_={
                "slug": stmt.excluded.slug,
                "title": stmt.excluded.title,
                "url": stmt.excluded.url,
                "difficulty": stmt.excluded.difficulty,
                "rating": stmt.excluded.rating,
                "tags": stmt.excluded.tags,
                "is_paid_only": stmt.excluded.is_paid_only,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        await session.execute(stmt)
        written += len(rows)
    await session.commit()
    return written


async def sync_codeforces_catalog(session: AsyncSession) -> int:
    async with CodeforcesClient() as client:
        problems = await client.fetch_problems()
    count = await _upsert_problems(session, Platform.codeforces, problems)
    logger.info("каталог Codeforces обновлён: %s задач", count)
    return count


async def sync_leetcode_catalog(session: AsyncSession) -> int:
    async with LeetCodeClient() as client:
        problems = await client.fetch_problems()
    count = await _upsert_problems(session, Platform.leetcode, problems)
    logger.info("каталог LeetCode обновлён: %s задач", count)
    return count


async def sync_catalog(session: AsyncSession, platform: Platform | None = None) -> dict[str, int]:
    result: dict[str, int] = {}
    targets = [platform] if platform else Platform.external()
    for target in targets:
        try:
            if target == Platform.codeforces:
                result[target.value] = await sync_codeforces_catalog(session)
            else:
                result[target.value] = await sync_leetcode_catalog(session)
        except Exception:  # каталог одной платформы не должен ронять вторую
            logger.exception("не удалось обновить каталог %s", target.value)
            result[target.value] = 0
    await set_state(session, "catalog_synced_at", utcnow().isoformat())
    return result


async def problem_count(session: AsyncSession, platform: Platform) -> int:
    from sqlalchemy import func

    stmt = select(func.count()).select_from(Problem).where(Problem.platform == platform)
    return int((await session.execute(stmt)).scalar_one())


async def get_state(session: AsyncSession, key: str) -> str | None:
    row = await session.get(SyncState, key)
    return row.value if row else None


async def set_state(session: AsyncSession, key: str, value: str | None) -> None:
    stmt = sqlite_insert(SyncState).values(key=key, value=value, updated_at=utcnow())
    stmt = stmt.on_conflict_do_update(
        index_elements=[SyncState.key],
        set_={"value": stmt.excluded.value, "updated_at": stmt.excluded.updated_at},
    )
    await session.execute(stmt)
    await session.commit()
