from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta

from app.config import settings
from app.db import SessionLocal
from app.models import utcnow
from app.notify import announce_started, send_deadline_reminders, send_due_reminders
from app.services import course
from app.services.catalog import get_state, set_state, sync_catalog
from app.services.sync import relink_orphan_submissions, sync_all

logger = logging.getLogger(__name__)

CATALOG_KEY = "catalog_synced_at"
COURSE_KEY = "course_synced_at"


async def _is_stale(key: str, hours: int) -> bool:
    async with SessionLocal() as session:
        raw = await get_state(session, key)
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        return True
    return utcnow() - last > timedelta(hours=hours)


async def refresh_course_if_stale() -> None:
    """Материалы курса — из репозитория преподавателей в том данных.

    Ошибку источника глотаем: недоступный Gitea не должен останавливать
    синхронизацию посылок и напоминания. В логе она останется.
    """
    if not settings.course_source_url:
        return
    if not await _is_stale(COURSE_KEY, settings.course_refresh_hours):
        return
    try:
        files = await course.refresh()
    except course.CourseSourceError as exc:
        logger.warning("материалы курса не обновились: %s", exc)
        return
    async with SessionLocal() as session:
        await set_state(session, COURSE_KEY, utcnow().isoformat())
    logger.info("материалы курса: файлов %s", files)


async def _catalog_is_stale() -> bool:
    async with SessionLocal() as session:
        raw = await get_state(session, CATALOG_KEY)
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        return True
    return utcnow() - last > timedelta(hours=settings.catalog_refresh_hours)


async def run_once() -> None:
    if await _catalog_is_stale():
        async with SessionLocal() as session:
            counts = await sync_catalog(session)
            logger.info("каталог обновлён: %s", counts)
            fixed = await relink_orphan_submissions(session)
            if fixed:
                logger.info("подвязано посылок к задачам: %s", fixed)

    await refresh_course_if_stale()

    async with SessionLocal() as session:
        added = await sync_all(session)
        if added:
            logger.info("новых посылок: %s", added)
        reminded = await send_due_reminders(session)
        if reminded:
            logger.info("отправлено напоминаний о событиях: %s", reminded)
        nudged = await send_deadline_reminders(session)
        if nudged:
            logger.info("отправлено напоминаний о дедлайне: %s", nudged)


# Объявления о начавшихся заданиях проверяем чаще синхронизации: контрольная
# в 10:00 должна прийти в Telegram в 10:00, а не в 10:09.
ANNOUNCE_EVERY = 60


async def announce_once() -> None:
    async with SessionLocal() as session:
        sent = await announce_started(session)
    if sent:
        logger.info("объявлено о начавшихся заданиях: %s", sent)


async def run_scheduler(stop_event: asyncio.Event) -> None:
    logger.info("планировщик запущен, интервал %s с", settings.sync_interval_seconds)
    # Небольшая задержка на старте, чтобы не конкурировать с первыми запросами.
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop_event.wait(), timeout=5)

    tick = min(ANNOUNCE_EVERY, settings.sync_interval_seconds)
    last_full: datetime | None = None
    while not stop_event.is_set():
        now = utcnow()
        jobs = [announce_once]
        if last_full is None or (now - last_full).total_seconds() >= settings.sync_interval_seconds:
            jobs.append(run_once)
            last_full = now
        for job in jobs:
            try:
                await job()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("итерация планировщика упала: %s", job.__name__)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=tick)
    logger.info("планировщик остановлен")
