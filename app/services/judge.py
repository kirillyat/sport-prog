"""Запуск решений в judge0.

Судья поднимается только на время контрольной и доступен по голому IP без
домена. Адрес поэтому живёт в базе, а не в окружении: виртуалку поднимают
за час до контрольной и гасят после неё, и портал не должен ради этого
перезапускаться. `JUDGE0_URL` из окружения остаётся запасным вариантом для
стенда, где судья стоит постоянно.

Адреса нет — запуск недоступен: портал по-прежнему принимает код, но
вердикта не ставит и честно об этом говорит, вместо того чтобы притворяться,
будто проверил.

Тесты гоняются подряд, по одному запросу на тест: наборы маленькие, а один
общий запрос не дал бы понять, на каком тесте решение упало.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import TaskTest
from app.services.catalog import get_state, set_state

logger = logging.getLogger(__name__)

# Вердикты judge0: 3 — «принято», остальное так или иначе отказ.
ACCEPTED = 3

URL_KEY = "judge0_url"
TOKEN_KEY = "judge0_token"


class JudgeUnavailable(RuntimeError):
    """Судья не настроен или не отвечает."""


@dataclass(slots=True)
class TestResult:
    position: int
    is_open: bool
    passed: bool
    status: str
    time_ms: int | None = None
    stdout: str = ""
    stderr: str = ""


@dataclass(slots=True)
class RunResult:
    results: list[TestResult]

    @property
    def passed(self) -> bool:
        return bool(self.results) and all(r.passed for r in self.results)

    @property
    def first_failed(self) -> TestResult | None:
        return next((r for r in self.results if not r.passed), None)

    @property
    def summary(self) -> str:
        """Короткая строка для вердикта посылки."""
        if self.passed:
            return "Принято"
        failed = self.first_failed
        if failed is None:
            return "Тестов нет"
        number = failed.position + 1
        return f"{failed.status} на тесте {number}"


@dataclass(frozen=True, slots=True)
class Config:
    url: str = ""
    token: str = ""

    @property
    def ready(self) -> bool:
        return bool(self.url)


async def config(session: AsyncSession) -> Config:
    """Куда стучаться. Записанное в базе главнее окружения — в том числе
    пустая строка: так «Отключить» гасит судью и там, где он задан в .env."""
    stored_url = await get_state(session, URL_KEY)
    stored_token = await get_state(session, TOKEN_KEY)
    url = settings.judge0_url if stored_url is None else stored_url
    token = settings.judge0_token if stored_token is None else stored_token
    return Config(url=url.strip(), token=token.strip())


async def connect(session: AsyncSession, url: str, token: str = "") -> Config:
    await set_state(session, URL_KEY, url.strip())
    await set_state(session, TOKEN_KEY, token.strip())
    return await config(session)


async def disconnect(session: AsyncSession) -> None:
    """Пустая строка, а не удаление строки: отсутствие записи вернуло бы
    адрес из окружения, а преподаватель просил судью выключить."""
    await set_state(session, URL_KEY, "")
    await set_state(session, TOKEN_KEY, "")


def _headers(cfg: Config) -> dict[str, str]:
    return {"X-Auth-Token": cfg.token} if cfg.token else {}


async def ping(cfg: Config) -> str:
    """Отвечает ли судья. Возвращает, чем он представился."""
    if not cfg.ready:
        raise JudgeUnavailable("адрес не задан")
    try:
        async with httpx.AsyncClient(timeout=settings.judge0_timeout) as client:
            response = await client.get(
                f"{cfg.url.rstrip('/')}/about", headers=_headers(cfg)
            )
    except httpx.HTTPError as exc:
        raise JudgeUnavailable(f"судья недоступен: {exc}") from exc
    if response.status_code >= 400:
        raise JudgeUnavailable(f"судья ответил {response.status_code}")
    about = response.json()
    return f"{about.get('version', '?')}"


async def run(
    cfg: Config, code: str, tests: list[TaskTest], time_limit_ms: int, memory_limit_mb: int
) -> RunResult:
    """Прогоняет код по тестам. Падение судьи — исключение, а не пустой результат."""
    if not cfg.ready:
        raise JudgeUnavailable("сервер проверки не настроен")

    base = cfg.url.rstrip("/")
    headers = _headers(cfg)
    results: list[TestResult] = []
    try:
        async with httpx.AsyncClient(timeout=settings.judge0_timeout) as client:
            for test in tests:
                payload = {
                    "source_code": code,
                    "language_id": settings.judge0_language_id,
                    "stdin": test.stdin,
                    "expected_output": test.expected,
                    "cpu_time_limit": round(time_limit_ms / 1000, 2),
                    "memory_limit": memory_limit_mb * 1024,
                }
                response = await client.post(
                    f"{base}/submissions",
                    params={"base64_encoded": "false", "wait": "true"},
                    json=payload,
                    headers=headers,
                )
                if response.status_code >= 400:
                    raise JudgeUnavailable(f"судья ответил {response.status_code}")
                results.append(_result(test, response.json()))
    except httpx.HTTPError as exc:
        raise JudgeUnavailable(f"судья недоступен: {exc}") from exc
    return RunResult(results=results)


def _result(test: TaskTest, data: dict) -> TestResult:
    status = data.get("status") or {}
    status_id = status.get("id")
    # Описание берём от судьи: «Wrong Answer», «Time Limit Exceeded» и прочие.
    description = status.get("description") or "Неизвестно"
    seconds = data.get("time")
    return TestResult(
        position=test.position,
        is_open=test.is_open,
        passed=status_id == ACCEPTED,
        status=description,
        time_ms=int(float(seconds) * 1000) if seconds else None,
        # Вывод показываем только по открытым тестам: закрытые не должны
        # утекать в интерфейс ни ответом, ни сообщением об ошибке.
        stdout=(data.get("stdout") or "")[:2000] if test.is_open else "",
        stderr=(data.get("stderr") or data.get("compile_output") or "")[:2000],
    )
