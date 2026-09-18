from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import SessionLocal
from app.models import Platform, PlatformAccount, Problem, Submission, utcnow
from app.platforms import CodeforcesClient, LeetCodeClient
from app.platforms.base import PlatformError, RemoteSubmission

logger = logging.getLogger(__name__)

# При первой синхронизации тянем историю поглубже, чтобы понимать,
# какие задачи студент уже решал до прихода в портал.
CF_BACKFILL_TOTAL = 3000
CF_BACKFILL_PAGE = 1000
CF_INCREMENTAL = 300


async def _fetch_codeforces(account: PlatformAccount) -> list[RemoteSubmission]:
    async with CodeforcesClient() as client:
        if account.last_synced_at is None:
            collected: list[RemoteSubmission] = []
            offset = 1
            while offset <= CF_BACKFILL_TOTAL:
                page = await client.fetch_submissions(
                    account.handle, count=CF_BACKFILL_PAGE, offset=offset
                )
                collected.extend(page)
                if len(page) < CF_BACKFILL_PAGE:
                    break
                offset += CF_BACKFILL_PAGE
            return collected
        return await client.fetch_submissions(account.handle, count=CF_INCREMENTAL)


async def _fetch_leetcode(account: PlatformAccount) -> list[RemoteSubmission]:
    async with LeetCodeClient() as client:
        return await client.fetch_submissions(account.handle)


async def _resolve_problem_ids(
    session: AsyncSession, platform: Platform, submissions: list[RemoteSubmission]
) -> dict[str, int]:
    """Ключ — external_id для Codeforces и slug для LeetCode."""
    if platform == Platform.codeforces:
        keys = {s.problem_external_id for s in submissions if s.problem_external_id}
        if not keys:
            return {}
        rows = await session.execute(
            select(Problem.external_id, Problem.id).where(
                Problem.platform == platform, Problem.external_id.in_(keys)
            )
        )
    else:
        keys = {s.problem_slug for s in submissions if s.problem_slug}
        if not keys:
            return {}
        rows = await session.execute(
            select(Problem.slug, Problem.id).where(
                Problem.platform == platform, Problem.slug.in_(keys)
            )
        )
    return {key: pid for key, pid in rows.all()}


async def sync_account(session: AsyncSession, account: PlatformAccount) -> int:
    """Затягивает посылки одного аккаунта. Возвращает число новых записей."""
    if not account.is_verified:
        return 0

    try:
        if account.platform == Platform.codeforces:
            submissions = await _fetch_codeforces(account)
        else:
            submissions = await _fetch_leetcode(account)
    except PlatformError as exc:
        account.last_sync_error = str(exc)[:500]
        await session.commit()
        logger.warning("синк %s/%s не удался: %s", account.platform.value, account.handle, exc)
        return 0

    if not submissions:
        account.last_synced_at = utcnow()
        account.last_sync_error = None
        await session.commit()
        return 0

    problem_ids = await _resolve_problem_ids(session, account.platform, submissions)

    rows = []
    for s in submissions:
        key = s.problem_external_id if account.platform == Platform.codeforces else s.problem_slug
        rows.append(
            {
                "user_id": account.user_id,
                "platform_account_id": account.id,
                "platform": account.platform.value,
                "external_id": s.external_id,
                "problem_id": problem_ids.get(key) if key else None,
                "problem_slug": s.problem_slug,
                "problem_title": s.problem_title,
                "verdict": s.verdict,
                "is_accepted": s.is_accepted,
                "language": s.language,
                "submitted_at": s.submitted_at,
                "created_at": utcnow(),
            }
        )

    inserted = 0
    for start in range(0, len(rows), 400):
        chunk = rows[start : start + 400]
        stmt = sqlite_insert(Submission).values(chunk)
        # Посылка неизменяема: если уже видели — просто пропускаем.
        stmt = stmt.on_conflict_do_nothing(
            index_elements=[Submission.platform, Submission.external_id]
        )
        result = await session.execute(stmt)
        inserted += result.rowcount or 0

    account.last_synced_at = utcnow()
    account.last_sync_error = None
    await session.commit()
    logger.info(
        "синк %s/%s: +%s посылок", account.platform.value, account.handle, inserted
    )
    return inserted


async def accounts_due_for_sync(session: AsyncSession) -> list[PlatformAccount]:
    threshold = utcnow() - timedelta(seconds=settings.sync_stale_seconds)
    stmt = select(PlatformAccount).where(
        PlatformAccount.verified_at.is_not(None),
        (PlatformAccount.last_synced_at.is_(None)) | (PlatformAccount.last_synced_at < threshold),
    )
    return list((await session.execute(stmt)).scalars().all())


async def sync_users(session: AsyncSession, user_ids: list[int]) -> tuple[int, int]:
    """Обновляет подтверждённые аккаунты названных студентов.

    Возвращает (новых посылок, аккаунтов обработано).
    """
    if not user_ids:
        return 0, 0
    stmt = select(PlatformAccount).where(
        PlatformAccount.user_id.in_(user_ids), PlatformAccount.verified_at.is_not(None)
    )
    accounts = list((await session.execute(stmt)).scalars().all())
    added = 0
    for account in accounts:
        added += await sync_account(session, account)
    return added, len(accounts)


async def sync_users_in_background(user_ids: list[int]) -> None:
    """Та же работа, но своей сессией: запрос не должен ждать минуту.

    Площадки держат паузу между запросами (Codeforces — две секунды), поэтому
    группа из двадцати человек обновляется около минуты. Держать всё это время
    открытым HTTP-запрос преподавателя нельзя.
    """
    async with SessionLocal() as session:
        added, accounts = await sync_users(session, user_ids)
    logger.info("массовое обновление: аккаунтов %s, новых посылок %s", accounts, added)


async def sync_all(session: AsyncSession) -> int:
    total = 0
    for account in await accounts_due_for_sync(session):
        total += await sync_account(session, account)
    return total


async def relink_orphan_submissions(session: AsyncSession) -> int:
    """Подвязывает посылки, пришедшие раньше, чем задача появилась в каталоге."""
    orphans = list(
        (
            await session.execute(
                select(Submission).where(
                    Submission.problem_id.is_(None), Submission.problem_slug.is_not(None)
                )
            )
        )
        .scalars()
        .all()
    )
    if not orphans:
        return 0

    fixed = 0
    for platform in Platform:
        subset = [s for s in orphans if s.platform == platform]
        if not subset:
            continue
        slugs = {s.problem_slug for s in subset if s.problem_slug}
        rows = await session.execute(
            select(Problem.slug, Problem.id).where(
                Problem.platform == platform, Problem.slug.in_(slugs)
            )
        )
        lookup = {slug: pid for slug, pid in rows.all()}
        for s in subset:
            pid = lookup.get(s.problem_slug or "")
            if pid:
                s.problem_id = pid
                fixed += 1
    if fixed:
        await session.commit()
    return fixed
