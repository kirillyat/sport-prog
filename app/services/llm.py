"""Языковая модель, подключённая к порталу: адрес, ключ, имя модели.

Пока только подключение и проверка связи — студентам модель не видна.
Говорим на OpenAI-совместимом API: так отдают модели и облачные сервисы
(DeepSeek, Mistral), и всё, что разворачивают у себя (vLLM, Ollama,
llama.cpp). Своя библиотека под каждого не нужна.

Хранится как адрес судьи — в служебной таблице, а не в окружении:
подключить модель или сменить её можно без перезапуска портала.
Окружение (`LLM_URL`, `LLM_TOKEN`, `LLM_MODEL`) — запасной вариант для
стенда, где модель стоит постоянно.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import utcnow
from app.services.catalog import get_state, set_state

URL_KEY = "llm_url"
TOKEN_KEY = "llm_token"
MODEL_KEY = "llm_model"
STATUS_KEY = "llm_status"
CHECKED_KEY = "llm_checked_at"


class ModelUnavailable(RuntimeError):
    """Модель не настроена или не отвечает."""


@dataclass(frozen=True, slots=True)
class Config:
    url: str = ""
    token: str = ""
    model: str = ""

    @property
    def ready(self) -> bool:
        return bool(self.url and self.model)


async def config(session: AsyncSession) -> Config:
    """Записанное в базе главнее окружения — в том числе пустое: так «Отключить»
    выключает модель и там, где она задана в .env."""

    async def pick(key: str, fallback: str) -> str:
        stored = await get_state(session, key)
        return (fallback if stored is None else stored).strip()

    return Config(
        url=await pick(URL_KEY, settings.llm_url),
        token=await pick(TOKEN_KEY, settings.llm_token),
        model=await pick(MODEL_KEY, settings.llm_model),
    )


async def connect(session: AsyncSession, url: str, model: str, token: str = "") -> Config:
    """Пустой ключ оставляет прежний: поле ключа на странице не показывает
    сохранённое, и смена модели не должна стирать ключ незаметно."""
    await set_state(session, URL_KEY, url.strip())
    await set_state(session, MODEL_KEY, model.strip())
    if token.strip():
        await set_state(session, TOKEN_KEY, token.strip())
    await _forget_health(session)
    return await config(session)


async def disconnect(session: AsyncSession) -> None:
    for key in (URL_KEY, TOKEN_KEY, MODEL_KEY):
        await set_state(session, key, "")
    await _forget_health(session)


@dataclass(frozen=True, slots=True)
class Health:
    checked_at: datetime | None = None
    detail: str = ""

    @property
    def known(self) -> bool:
        return self.checked_at is not None

    @property
    def ok(self) -> bool:
        return self.known and not self.detail


async def _forget_health(session: AsyncSession) -> None:
    for key in (STATUS_KEY, CHECKED_KEY):
        await set_state(session, key, None)


async def health(session: AsyncSession) -> Health:
    raw = await get_state(session, CHECKED_KEY)
    if not raw:
        return Health()
    try:
        checked_at = datetime.fromisoformat(raw)
    except ValueError:
        return Health()
    return Health(checked_at=checked_at, detail=await get_state(session, STATUS_KEY) or "")


async def models(cfg: Config) -> list[str]:
    """Какие модели отдаёт сервер (`GET /models`)."""
    if not cfg.url:
        raise ModelUnavailable("адрес не задан")
    headers = {"Authorization": f"Bearer {cfg.token}"} if cfg.token else {}
    try:
        async with httpx.AsyncClient(timeout=settings.judge0_ping_timeout) as client:
            response = await client.get(f"{cfg.url.rstrip('/')}/models", headers=headers)
    except httpx.TimeoutException as exc:
        raise ModelUnavailable("не ответил вовремя") from exc
    except httpx.HTTPError as exc:
        raise ModelUnavailable(f"недоступен: {str(exc) or type(exc).__name__}") from exc
    if response.status_code in (401, 403):
        raise ModelUnavailable("ключ не подошёл")
    if response.status_code >= 400:
        raise ModelUnavailable(f"ответил {response.status_code}")
    try:
        return [str(item["id"]) for item in response.json().get("data", [])]
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ModelUnavailable("ответ не похож на OpenAI-совместимый API") from exc


async def check(session: AsyncSession) -> tuple[Config, Health]:
    """Позвонить серверу и записать, что вышло. Модель ищем в его списке:
    опечатка в имени модели — самая частая причина, почему «не работает»."""
    cfg = await config(session)
    if not cfg.url:
        await _forget_health(session)
        return cfg, Health()
    try:
        names = await models(cfg)
    except ModelUnavailable as exc:
        detail = str(exc)
    else:
        detail = "" if not names or cfg.model in names else f"модели «{cfg.model}» у сервера нет"
    await set_state(session, STATUS_KEY, detail)
    await set_state(session, CHECKED_KEY, utcnow().isoformat())
    return cfg, await health(session)
