"""Telegram-бот для входа в портал.

Long polling, без вебхуков и без публичного домена — поэтому работает
одинаково на ноутбуке и на сервере. Штатный Telegram Login Widget требует
HTTPS-домен, привязанный к боту, и на localhost не заводится.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.i18n import mark as N_
from app.i18n import translate as _
from app.models import LoginToken, utcnow
from app.services.catalog import get_state, set_state

logger = logging.getLogger(__name__)

OFFSET_KEY = "telegram_update_offset"
POLL_TIMEOUT = 25

WELCOME = (
    N_("Привет! Этот бот подтверждает вход в портал подготовки к олимпиадам.\n\n"
       "Открой страницу входа на сайте и нажми кнопку «Войти через Telegram» — "
       "оттуда ты попадёшь сюда с одноразовым кодом.")
)


class TelegramAPI:
    def __init__(self, token: str) -> None:
        self._base = f"https://api.telegram.org/bot{token}"
        self._client = httpx.AsyncClient(timeout=POLL_TIMEOUT + 10)

    async def close(self) -> None:
        await self._client.aclose()

    async def call(self, method: str, **payload) -> dict | None:
        try:
            response = await self._client.post(f"{self._base}/{method}", json=payload)
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Telegram %s: %s", method, exc)
            return None
        if not data.get("ok"):
            logger.warning("Telegram %s отказал: %s", method, data.get("description"))
            return None
        return data.get("result")

    async def upload(self, method: str, files: dict, **payload) -> dict | None:
        """Отправка файла: multipart вместо JSON, всё остальное так же."""
        try:
            response = await self._client.post(
                f"{self._base}/{method}", data=payload, files=files, timeout=180
            )
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Telegram %s: %s", method, exc)
            return None
        if not data.get("ok"):
            logger.warning("Telegram %s отказал: %s", method, data.get("description"))
            return None
        return data.get("result")

    async def send_message(self, chat_id: int, text: str) -> None:
        await self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML")

    async def get_updates(self, offset: int | None) -> list[dict]:
        payload = {"timeout": POLL_TIMEOUT, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        return await self.call("getUpdates", **payload) or []


def _display_name(sender: dict) -> str:
    parts = [sender.get("first_name"), sender.get("last_name")]
    name = " ".join(p for p in parts if p).strip()
    return name or sender.get("username") or f"tg{sender.get('id')}"


async def _handle_start(api: TelegramAPI, sender: dict, chat_id: int, code: str) -> None:
    async with SessionLocal() as session:
        token = await session.scalar(select(LoginToken).where(LoginToken.code == code))
        now = utcnow()
        if token is None:
            await api.send_message(chat_id, _("Код не найден. Открой страницу входа заново."))
            return
        if token.consumed_at is not None:
            await api.send_message(chat_id, _("Этот код уже использован. Запроси новый."))
            return
        if token.expires_at < now:
            await api.send_message(chat_id, _("Код истёк. Открой страницу входа заново."))
            return

        token.telegram_id = sender["id"]
        token.telegram_username = sender.get("username")
        token.display_name = _display_name(sender)
        token.confirmed_at = now
        await session.commit()

    await api.send_message(
        chat_id,
        _("Вход подтверждён ✅ Возвращайся на вкладку с сайтом — она откроется сама.\n\n"
          "Если ты <b>не</b> открывал страницу входа — значит, код прислал кто-то другой. "
          "Напиши преподавателю."),
    )


async def _handle_update(api: TelegramAPI, update: dict) -> None:
    message = update.get("message") or {}
    text = (message.get("text") or "").strip()
    sender = message.get("from") or {}
    chat_id = (message.get("chat") or {}).get("id")
    if not chat_id or not sender.get("id") or not text:
        return

    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        if len(parts) == 2 and parts[1].strip():
            await _handle_start(api, sender, chat_id, parts[1].strip())
        else:
            await api.send_message(chat_id, _(WELCOME))
        return

    if text.startswith("/help"):
        await api.send_message(chat_id, _(WELCOME))
        return

    if text.startswith("/id"):
        lines = [_("Твой telegram id: <code>%(id)s</code>") % {"id": sender["id"]}]
        if (message.get("chat") or {}).get("type") != "private":
            lines.append(
                _("Id этого чата: <code>%(id)s</code> — впиши его в настройках группы на портале.")
                % {"id": chat_id}
            )
        await api.send_message(chat_id, "\n".join(lines))


async def run_bot(stop_event: asyncio.Event) -> None:
    if not settings.telegram_bot_token:
        logger.warning("TELEGRAM_BOT_TOKEN не задан — бот не запущен")
        return

    api = TelegramAPI(settings.telegram_bot_token)
    me = await api.call("getMe")
    if me:
        logger.info("Telegram-бот запущен: @%s", me.get("username"))

    async with SessionLocal() as session:
        raw_offset = await get_state(session, OFFSET_KEY)
    offset = int(raw_offset) if raw_offset and raw_offset.isdigit() else None

    try:
        while not stop_event.is_set():
            updates = await api.get_updates(offset)
            if not updates:
                if stop_event.is_set():
                    break
                continue
            for update in updates:
                try:
                    await _handle_update(api, update)
                except Exception:
                    logger.exception("не удалось обработать апдейт %s", update.get("update_id"))
                offset = update["update_id"] + 1
            async with SessionLocal() as session:
                await set_state(session, OFFSET_KEY, str(offset))
    except asyncio.CancelledError:
        raise
    finally:
        await api.close()
        logger.info("Telegram-бот остановлен")
