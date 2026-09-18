from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime


class PlatformError(RuntimeError):
    """Платформа недоступна или ответила не тем, чего мы ждали."""


class UserNotFound(PlatformError):
    """Такого хэндла/username на платформе нет."""


@dataclass(slots=True)
class RemoteProblem:
    external_id: str
    slug: str
    title: str
    url: str
    difficulty: str | None = None
    rating: int | None = None
    tags: list[str] = field(default_factory=list)
    is_paid_only: bool = False


@dataclass(slots=True)
class RemoteSubmission:
    external_id: str
    problem_external_id: str | None
    problem_slug: str | None
    problem_title: str | None
    verdict: str | None
    is_accepted: bool
    submitted_at: datetime
    language: str | None = None


@dataclass(slots=True)
class RemoteProfile:
    handle: str
    display_name: str | None = None
    # Все текстовые поля профиля, в которых студент может разместить код верификации.
    searchable_text: str = ""


class RateLimiter:
    """Минимальный интервал между запросами к одной платформе.

    Codeforces просит не чаще одного запроса в 2 секунды, LeetCode
    официальных лимитов не публикует, но охотно отдаёт 429.

    Лимитер один на процесс и на площадку — см. `limiter_for`. Когда он лежал
    внутри клиента, а клиент заводился на каждый аккаунт, пауза не работала
    вовсе: у нового лимитера `_last` равен нулю, и первый запрос уходил
    немедленно. Обновление группы из тридцати человек выстреливало тридцать
    запросов подряд и получало от Codeforces «Call limit exceeded».

    Интервал спрашивается у настроек на каждом запросе, а не запоминается:
    иначе значение застывало бы на том, что было при импорте модуля.
    """

    def __init__(self, interval: Callable[[], float]) -> None:
        self._interval = interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def __aenter__(self) -> None:
        await self._lock.acquire()
        wait = self._interval() - (time.monotonic() - self._last)
        if wait > 0:
            await asyncio.sleep(wait)

    async def __aexit__(self, *exc_info) -> None:
        self._last = time.monotonic()
        self._lock.release()


# Общие лимитеры процесса. Ключ — название площадки, а не класс клиента:
# клиентов за один синк создаётся много, площадка остаётся одна.
_LIMITERS: dict[str, RateLimiter] = {}


def limiter_for(platform: str, interval: Callable[[], float]) -> RateLimiter:
    """Лимитер площадки. Один на процесс, сколько бы клиентов ни завели."""
    if platform not in _LIMITERS:
        _LIMITERS[platform] = RateLimiter(interval)
    return _LIMITERS[platform]
