"""Приём исходников посылок снаружи.

Портал не может забрать код сам: страницы Codeforces и LeetCode закрыты
Cloudflare для любых автоматических клиентов — проверено и с сервера, и из
управляемого браузера. Поэтому код приносит скрипт, который работает в
обычном браузере преподавателя, а портал только принимает и показывает.

Ручка защищена отдельным токеном, а не сессией: скрипт работает без
пользователя, и давать ему право говорить от чьего-то имени незачем.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.config import settings
from app.deps import SessionDep
from app.models import Platform, Submission, utcnow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ingest", tags=["ingest"], include_in_schema=False)

MAX_CODE_BYTES = 200_000


class SourcePayload(BaseModel):
    platform: Platform
    external_id: str = Field(max_length=64)
    code: str


def _check(token: str | None) -> None:
    if not settings.ingest_token:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Приём исходников выключен")
    if token != settings.ingest_token:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Неверный токен")


@router.get("/wanted")
async def wanted(session: SessionDep, x_ingest_token: str | None = Header(None), limit: int = 50):
    """Какие посылки ждут исходника: скрипт спрашивает, что качать."""
    _check(x_ingest_token)
    stmt = (
        select(Submission.platform, Submission.external_id, Submission.problem_slug)
        .where(Submission.is_accepted.is_(True), Submission.code.is_(None))
        .order_by(Submission.submitted_at.desc())
        .limit(min(limit, 200))
    )
    rows = (await session.execute(stmt)).all()
    return [
        {"platform": platform.value, "external_id": external_id, "problem_slug": slug}
        for platform, external_id, slug in rows
    ]


@router.post("/source")
async def put_source(
    session: SessionDep, payload: SourcePayload, x_ingest_token: str | None = Header(None)
):
    """Кладём исходник к посылке. Повторная отправка перезаписывает."""
    _check(x_ingest_token)
    if len(payload.code.encode("utf-8")) > MAX_CODE_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Слишком длинный исходник")

    submission = await session.scalar(
        select(Submission).where(
            Submission.platform == payload.platform,
            Submission.external_id == payload.external_id,
        )
    )
    if submission is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Посылка не найдена")

    submission.code = payload.code
    submission.code_fetched_at = utcnow()
    await session.commit()
    logger.info("получен исходник посылки %s/%s", payload.platform.value, payload.external_id)
    return {"ok": True}
