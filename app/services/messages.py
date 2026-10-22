"""Сообщения преподавателя к заданию: короткий чат в одну сторону.

Зачем: ссылку на комнату для совместного решения (LeetCode, Codeforces —
что угодно) раньше кидали в мессенджер, а он есть не у всех на компьютере
в аудитории. Портал открыт у всех — значит, ссылка должна быть там.

Видят сообщения те же, кто видит задание: правило одно,
`may_see_assignment`. Свежие ещё несколько часов висят на главной — чтобы
студент, только открывший портал на паре, сразу нашёл ссылку. Новые
подтягивает скрипт страницы (`app/static/messages.js`), без перезагрузки.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import notify
from app.i18n import translate as _
from app.models import Assignment, AssignmentMessage, User, utcnow
from app.services.progress import assignments_for_user

MAX_CHARS = 1000
# Сколько показываем на странице задания: это чат пары, а не архив.
LIMIT = 30
# Сколько сообщение висит на главной: пара идёт полтора часа, с запасом.
FRESH = timedelta(hours=3)
FRESH_LIMIT = 3

def clean(body: str) -> str:
    lines = [" ".join(line.split()) for line in body.replace("\r\n", "\n").split("\n")]
    return "\n".join(line for line in lines if line).strip()


async def post(
    session: AsyncSession,
    assignment: Assignment,
    author: User,
    body: str,
    *,
    telegram: bool = False,
) -> AssignmentMessage | str:
    """Сообщение участникам задания. Строка вместо сообщения — текст ошибки.

    В Telegram — только по просьбе: сообщения на паре затем и пишутся на
    портале, чтобы не дёргать мессенджер.
    """
    text = clean(body)
    if not text:
        return _("Пустое сообщение")
    if len(text) > MAX_CHARS:
        return _("Не длиннее %(limit)s символов") % {"limit": MAX_CHARS}
    message = AssignmentMessage(assignment_id=assignment.id, author_id=author.id, body=text)
    session.add(message)
    await session.commit()
    await session.refresh(message)
    if telegram:
        await notify.notify_assignment_message(message, assignment, session)
    return message


async def for_assignment(
    session: AsyncSession, assignment_id: int, after: int = 0, limit: int = LIMIT
) -> list[AssignmentMessage]:
    """Сообщения задания, свежие сверху; `after` — только новее этого id."""
    rows = await session.execute(
        select(AssignmentMessage)
        .where(AssignmentMessage.assignment_id == assignment_id, AssignmentMessage.id > after)
        .order_by(AssignmentMessage.id.desc())
        .limit(limit)
    )
    return list(rows.scalars())


async def fresh_for(
    session: AsyncSession, user: User, after: int = 0, now: datetime | None = None
) -> list[AssignmentMessage]:
    """Свежие сообщения из заданий, которые человек видит, — для главной."""
    ids = [a.id for a in await assignments_for_user(session, user)]
    if not ids:
        return []
    since = (now or utcnow()) - FRESH
    rows = await session.execute(
        select(AssignmentMessage)
        .where(
            AssignmentMessage.assignment_id.in_(ids),
            AssignmentMessage.created_at >= since,
            AssignmentMessage.id > after,
        )
        .order_by(AssignmentMessage.id.desc())
        .limit(FRESH_LIMIT)
    )
    return list(rows.scalars())


async def delete(session: AsyncSession, assignment_id: int, message_id: int) -> None:
    """Удаляет сообщение задания; чужого задания или уже удалённого — нет."""
    await session.execute(
        sa_delete(AssignmentMessage).where(
            AssignmentMessage.id == message_id,
            AssignmentMessage.assignment_id == assignment_id,
        )
    )
    await session.commit()
