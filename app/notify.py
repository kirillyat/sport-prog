"""Уведомления в Telegram.

Адресат выбирается так: если у группы (или общий) задан чат — пишем
туда одним сообщением, иначе бот пишет каждому участнику лично. Личное задание
всегда уходит только самому студенту.

Отправка никогда не роняет запрос: ошибка сети — это запись в лог,
а объявление всё равно опубликовано.
"""

from __future__ import annotations

import html
import json
import logging
from collections.abc import Iterable
from datetime import timedelta
from functools import partial
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import TelegramAPI
from app.config import settings
from app.i18n import translate as _
from app.models import (
    Announcement,
    Assignment,
    Group,
    GroupMembership,
    Material,
    User,
    utcnow,
)
from app.services.progress import compute_progress, participants_for_assignment
from app.templating import fmt_dt, plural

logger = logging.getLogger(__name__)

# Бот вправе отправить документ до 50 МБ — наш предел на загрузку и так меньше.
TELEGRAM_MAX_DOCUMENT = 50 * 1024 * 1024
CAPTION_LIMIT = 1024

# Кнопку с такой ссылкой Telegram не примет, да и открыть её с телефона нельзя.
LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", ""}


def button_markup(label: str, url: str) -> str | None:
    """Кнопка под сообщением. None — ссылка нерабочая, обойдёмся текстом."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or parsed.hostname in LOCAL_HOSTS:
        return None
    return json.dumps(
        {"inline_keyboard": [[{"text": label, "url": url}]]}, ensure_ascii=False
    )


def with_link(text: str, label: str, url: str) -> str:
    """Запасной вариант, когда кнопки не будет: ссылка прямо в тексте."""
    return f'{text}\n<a href="{_e(url)}">{_e(label)}</a>'


async def _attempt(
    send, button: tuple[str, str] | None, field: str, text: str,
    limit: int | None = None, **payload,
) -> dict | None:
    """Одна отправка с кнопкой под сообщением.

    Кнопку Telegram может не принять — например, адрес портала ещё локальный.
    Тогда повторяем без неё, положив ссылку прямо в текст: лучше ссылка,
    чем сообщение, из которого некуда нажать.
    """
    def fit(value: str) -> str:
        return value[:limit] if limit else value

    markup = button_markup(*button) if button else None
    body = {**payload, "parse_mode": "HTML"}
    body[field] = fit(text if markup or not button else with_link(text, *button))
    if markup:
        body["reply_markup"] = markup

    result = await send(**body)
    if result is None and markup:
        body.pop("reply_markup")
        body[field] = fit(with_link(text, *button))
        result = await send(**body)
    return result


def _chat_for(group: Group | None) -> str | None:
    if group is not None and group.telegram_chat_id:
        return group.telegram_chat_id
    return settings.telegram_notify_chat_id or None


async def _group_of(session: AsyncSession | None, group_id: int | None) -> Group | None:
    """Связь после refresh не загружена; тянем группу явно, чтобы не упасть в async."""
    if session is None or group_id is None:
        return None
    return await session.get(Group, group_id)


async def _personal_chats(
    session: AsyncSession, group_id: int | None = None, user_id: int | None = None
) -> list[int]:
    """Личные чаты адресатов. Кто не входил через бота, тому написать некуда."""
    stmt = select(User.telegram_id).where(
        User.telegram_id.is_not(None), User.is_active.is_(True)
    )
    if user_id is not None:
        stmt = stmt.where(User.id == user_id)
    elif group_id is not None:
        stmt = stmt.join(GroupMembership, GroupMembership.user_id == User.id).where(
            GroupMembership.group_id == group_id
        )
    # Иначе адресат — все.
    return list((await session.execute(stmt)).scalars().all())


async def send_many(
    chat_ids: Iterable[str | int], text: str, button: tuple[str, str] | None = None
) -> int:
    """Одна рассылка — один HTTP-клиент. Недоступный адресат не отменяет остальных."""
    return await send_each([(chat_id, text) for chat_id in chat_ids], button)


async def send(chat_id: str | int, text: str) -> bool:
    return await send_many([chat_id], text) > 0


async def send_each(
    messages: Iterable[tuple[str | int, str]], button: tuple[str, str] | None = None
) -> int:
    """Каждому свой текст — одним клиентом. Так рассылаются напоминания:
    в них у каждого свои цифры, общим сообщением не обойтись."""
    items = list(messages)
    if not items or not settings.telegram_bot_token:
        return 0
    api = TelegramAPI(settings.telegram_bot_token)
    sent = 0
    try:
        for chat_id, text in items:
            result = await _attempt(
                partial(api.call, "sendMessage"), button, "text", text,
                chat_id=chat_id, disable_web_page_preview=True,
            )
            if result is not None:
                sent += 1
    finally:
        await api.close()
    return sent


async def _targets(
    session: AsyncSession | None,
    group_id: int | None = None,
    user_id: int | None = None,
) -> list[str | int]:
    """Общий чат, если он задан; иначе — личные чаты адресатов."""
    if user_id is None:
        chat = _chat_for(await _group_of(session, group_id))
        if chat:
            return [chat]
    if session is None:
        return []
    return list(await _personal_chats(session, group_id, user_id))


async def _deliver(
    session: AsyncSession | None,
    text: str,
    group_id: int | None = None,
    user_id: int | None = None,
    button: tuple[str, str] | None = None,
) -> int:
    return await send_many(await _targets(session, group_id, user_id), text, button)


async def send_document_many(
    chat_ids: Iterable[str | int], filename: str, content: bytes, caption: str,
    button: tuple[str, str] | None = None,
) -> int:
    """Файл заливается один раз: остальным он уходит по file_id, который вернул
    Telegram. Иначе рассылка на группу — это N одинаковых загрузок."""
    ids = list(chat_ids)
    if not ids or not settings.telegram_bot_token:
        return 0
    if len(content) > TELEGRAM_MAX_DOCUMENT:
        return 0

    api = TelegramAPI(settings.telegram_bot_token)
    sent = 0
    file_id: str | None = None
    try:
        for chat_id in ids:
            if file_id is None:
                # Первому файл уходит целиком, остальным — по file_id.
                send = partial(api.upload, "sendDocument", {"document": (filename, content)})
            else:
                send = partial(api.call, "sendDocument", document=file_id)
            result = await _attempt(
                send, button, "caption", caption, limit=CAPTION_LIMIT, chat_id=chat_id,
            )
            if file_id is None:
                file_id = ((result or {}).get("document") or {}).get("file_id")
            if result is not None:
                sent += 1
    finally:
        await api.close()
    return sent


def _e(value: object) -> str:
    return html.escape(str(value or ""))


def portal_url(path: str) -> str:
    return settings.base_url.rstrip("/") + path


def announcement_button(item: Announcement) -> tuple[str, str]:
    """У анонса своя ссылка (на контест), иначе ведём на портал."""
    if item.url:
        return (item.url_label or _("Перейти"), item.url)
    return (_("Открыть на портале"), portal_url("/announcements"))


def assignment_button(item: Assignment) -> tuple[str, str]:
    return (_("Открыть задание"), portal_url(f"/assignments/{item.id}"))


def material_button(item: Material) -> tuple[str, str]:
    return (_("Открыть на портале"), portal_url(f"/materials/{item.id}/view"))


def announcement_text(item: Announcement) -> str:
    lines = [f"📣 <b>{_e(item.title)}</b>"]
    if item.starts_at:
        when = fmt_dt(item.starts_at)
        if item.ends_at:
            when += f" — {fmt_dt(item.ends_at, '%H:%M')}"
        lines.append(f"🕐 {when}")
    if item.body:
        lines.append("")
        lines.append(_e(item.body))
    return "\n".join(lines)


def reminder_text(item: Announcement) -> str:
    minutes = max(1, round((item.starts_at - utcnow()).total_seconds() / 60))
    lines = [
        "⏰ " + _("Через %(minutes)s мин: <b>%(title)s</b>")
        % {"minutes": minutes, "title": _e(item.title)},
        "🕐 " + _("старт %(time)s") % {"time": fmt_dt(item.starts_at, "%H:%M")},
    ]
    return "\n".join(lines)


def assignment_text(item: Assignment, problems: int) -> str:
    lines = [
        "📝 " + _("Новое задание: <b>%(title)s</b>") % {"title": _e(item.title)},
        _("Задач: %(count)s") % {"count": problems},
    ]
    if item.deadline:
        suffix = " " + _("(после срока не засчитывается)") if item.hard_deadline else ""
        lines.append(_("Дедлайн: %(when)s") % {"when": fmt_dt(item.deadline)} + suffix)
    return "\n".join(lines)


def material_text(item: Material) -> str:
    lines = ["📘 " + _("Материал: <b>%(title)s</b>") % {"title": _e(item.title)}]
    if item.description:
        lines.append(_e(item.description))
    return "\n".join(lines)


async def notify_material(item: Material, session: AsyncSession | None = None) -> int:
    """Файл уходит прямо в чат: ноутбук удобнее получить, а не идти за ним.

    Не дошёл (велик, сеть, отказ) — отправляем хотя бы ссылку на портал.
    """
    targets = await _targets(session, group_id=item.group_id)
    if not targets:
        return 0

    content = _read_material(item)
    if content is not None:
        sent = await send_document_many(
            targets, item.filename, content, material_text(item), material_button(item)
        )
        if sent:
            return sent
    return await send_many(targets, material_text(item), material_button(item))


def _read_material(item: Material) -> bytes | None:
    from app.services import materials

    path = materials.path_for(item.stored_name)
    try:
        return path.read_bytes()
    except OSError as exc:
        logger.warning("Материал %s не прочитался: %s", item.id, exc)
        return None


async def notify_announcement(item: Announcement, session: AsyncSession | None = None) -> int:
    return await _deliver(
        session, announcement_text(item), group_id=item.group_id,
        button=announcement_button(item),
    )


async def notify_assignment(
    item: Assignment, problems: int, session: AsyncSession | None = None
) -> int:
    return await _deliver(
        session, assignment_text(item, problems),
        group_id=item.group_id, user_id=item.user_id, button=assignment_button(item),
    )


def deadline_reminder_text(item: Assignment, left: int, total: int) -> str:
    hours = max(1, round((item.deadline - utcnow()).total_seconds() / 3600))
    lines = [
        "⏰ " + _("Через %(count)s %(unit)s дедлайн: <b>%(title)s</b>")
        % {
            "count": hours,
            "unit": plural(hours, _("час|часа|часов")),
            "title": _e(item.title),
        },
        _("Осталось задач: %(left)s из %(total)s") % {"left": left, "total": total},
        _("Срок: %(when)s") % {"when": fmt_dt(item.deadline)},
    ]
    if item.hard_deadline:
        lines.append(_("После срока решения не засчитываются."))
    return "\n".join(lines)


async def send_deadline_reminders(session: AsyncSession) -> int:
    """Личное напоминание тем, кто не закрыл задание. Кто закрыл — не трогаем.

    Всегда лично: в напоминании у каждого свои цифры, в общий чат такое
    не отправишь.
    """
    now = utcnow()
    horizon = now + timedelta(hours=settings.assignment_reminder_hours)
    stmt = select(Assignment).where(
        Assignment.deadline.is_not(None),
        Assignment.deadline > now,
        Assignment.deadline <= horizon,
        Assignment.reminded_at.is_(None),
    )
    # Кнопка ведёт на своё задание, поэтому рассылка идёт по заданиям.
    grouped: dict[Assignment, list[tuple[str | int, str]]] = {}
    for assignment in (await session.execute(stmt)).scalars().all():
        # Отмечаем в любом случае: иначе каждый круг будем перебирать одно и то же.
        assignment.reminded_at = now
        participants = await participants_for_assignment(session, assignment)
        progress = await compute_progress(session, assignment, participants)
        total = progress.total_problems
        for user in participants:
            left = total - progress.solved_count(user.id)
            if left <= 0 or user.telegram_id is None or user.is_teacher:
                continue
            grouped.setdefault(assignment, []).append(
                (user.telegram_id, deadline_reminder_text(assignment, left, total))
            )

    sent = 0
    for assignment, batch in grouped.items():
        sent += await send_each(batch, assignment_button(assignment))
    await session.commit()
    return sent


async def send_due_reminders(session: AsyncSession) -> int:
    """Напоминания о событиях, которые начнутся в ближайшие N минут."""
    now = utcnow()
    horizon = now + timedelta(minutes=settings.reminder_minutes_before)
    stmt = select(Announcement).where(
        Announcement.starts_at.is_not(None),
        Announcement.starts_at > now,
        Announcement.starts_at <= horizon,
        Announcement.reminded_at.is_(None),
    )
    sent = 0
    for item in (await session.execute(stmt)).scalars().all():
        # Отмечаем в любом случае: иначе каждый круг будем перебирать одно и то же.
        item.reminded_at = now
        if await _deliver(
            session, reminder_text(item), group_id=item.group_id,
            button=announcement_button(item),
        ):
            sent += 1
    await session.commit()
    return sent
