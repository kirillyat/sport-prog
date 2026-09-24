"""Запуск решений в judge0.

Судья поднимается только на время контрольной и доступен по голому IP без
домена. Адрес поэтому живёт в базе, а не в окружении: виртуалку поднимают
за час до контрольной и гасят после неё, и портал не должен ради этого
перезапускаться. `JUDGE0_URL` из окружения остаётся запасным вариантом для
стенда, где судья стоит постоянно.

Настроенность и доступность — разные вещи, и портал их не путает. Адрес
задан или снят преподавателем вручную; отвечает судья или нет — выясняется
на ходу и записывается рядом. Погашенная посреди контрольной виртуалка не
требует ничего отключать: портал сам скажет, что проверки нет, и сам заметит,
когда она вернётся.

Проверки нет — код всё равно принимаем, но вердикта не ставим и честно об
этом говорим, вместо того чтобы притворяться, будто проверили.

Тесты гоняются подряд, по одному запросу на тест: наборы маленькие, а один
общий запрос не дал бы понять, на каком тесте решение упало. Весь прогон
уложен в общий срок: медленный судья не должен держать студента в ожидании
бесконечно.

Тесты сюда приезжают простыми `Case`, а не строками базы: разговор с судьёй
длится секунды, и всё это время сессия базы должна быть отпущена.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import TaskTest, utcnow
from app.services.catalog import get_state, set_state

logger = logging.getLogger(__name__)

# Вердикты judge0: 3 — «принято», остальное так или иначе отказ.
ACCEPTED = 3

URL_KEY = "judge0_url"
TOKEN_KEY = "judge0_token"
# Чем судья ответил в последний раз: пусто — ответил, текст — причина отказа.
STATUS_KEY = "judge0_status"
CHECKED_KEY = "judge0_checked_at"
VERSION_KEY = "judge0_version"


class JudgeUnavailable(RuntimeError):
    """Судья не настроен или не отвечает."""


@dataclass(frozen=True, slots=True)
class Case:
    """Тест, отвязанный от базы: его можно держать, пока сессия закрыта."""

    position: int
    is_open: bool
    stdin: str
    expected: str


def cases(tests: Iterable[TaskTest]) -> list[Case]:
    return [
        Case(position=t.position, is_open=t.is_open, stdin=t.stdin, expected=t.expected)
        for t in tests
    ]


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
    # Прошлые отказы к новому адресу отношения не имеют.
    await _forget_health(session)
    return await config(session)


async def disconnect(session: AsyncSession) -> None:
    """Пустая строка, а не удаление строки: отсутствие записи вернуло бы
    адрес из окружения, а преподаватель просил судью выключить."""
    await set_state(session, URL_KEY, "")
    await set_state(session, TOKEN_KEY, "")
    await _forget_health(session)


@dataclass(frozen=True, slots=True)
class Health:
    """Что известно о доступности судьи. Отдельно от настройки: адрес может
    быть задан, а виртуалка — уже погашена."""

    checked_at: datetime | None = None
    detail: str = ""
    version: str = ""

    @property
    def known(self) -> bool:
        return self.checked_at is not None

    @property
    def ok(self) -> bool:
        return self.known and not self.detail


async def _forget_health(session: AsyncSession) -> None:
    for key in (STATUS_KEY, CHECKED_KEY, VERSION_KEY):
        await set_state(session, key, None)


async def note(session: AsyncSession, detail: str = "", version: str = "") -> None:
    """Запомнить, чем кончился разговор с судьёй. Пустая причина — ответил.

    Пишется после каждого прогона, поэтому погасшая посреди контрольной
    виртуалка видна преподавателю без отдельной проверки, а вернувшаяся
    отмечается сама — первым же удавшимся прогоном.
    """
    await set_state(session, STATUS_KEY, detail)
    await set_state(session, CHECKED_KEY, utcnow().isoformat())
    if version:
        await set_state(session, VERSION_KEY, version)


async def health(session: AsyncSession) -> Health:
    raw = await get_state(session, CHECKED_KEY)
    if not raw:
        return Health()
    try:
        checked_at = datetime.fromisoformat(raw)
    except ValueError:
        return Health()
    return Health(
        checked_at=checked_at,
        detail=await get_state(session, STATUS_KEY) or "",
        version=await get_state(session, VERSION_KEY) or "",
    )


async def check(session: AsyncSession) -> tuple[Config, Health]:
    """Позвонить судье и записать, что вышло."""
    cfg = await config(session)
    if not cfg.ready:
        await _forget_health(session)
        return cfg, Health()
    try:
        version = await ping(cfg)
    except JudgeUnavailable as exc:
        await note(session, str(exc))
    else:
        await note(session, version=version)
    return cfg, await health(session)


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
    cfg: Config, code: str, tests: list[Case], time_limit_ms: int, memory_limit_mb: int
) -> RunResult:
    """Прогоняет код по тестам. Падение судьи — исключение, а не пустой результат."""
    if not cfg.ready:
        raise JudgeUnavailable("сервер проверки не настроен")

    base = cfg.url.rstrip("/")
    headers = _headers(cfg)
    results: list[TestResult] = []
    deadline = time.monotonic() + settings.judge0_total_timeout
    try:
        async with httpx.AsyncClient(timeout=settings.judge0_timeout) as client:
            for test in tests:
                if time.monotonic() > deadline:
                    raise JudgeUnavailable(
                        f"проверка не уложилась в {int(settings.judge0_total_timeout)} с"
                    )
                payload = {
                    "source_code": code,
                    "language_id": settings.judge0_language_id,
                    "stdin": test.stdin,
                    "expected_output": test.expected,
                    "cpu_time_limit": round(time_limit_ms / 1000, 2),
                    # Процессорное время не ловит решение, которое спит или
                    # ждёт ввода: оно не считается, а воркер судьи занят.
                    "wall_time_limit": round(time_limit_ms / 1000 * 2 + 1, 2),
                    "memory_limit": memory_limit_mb * 1024,
                    # Контрольная: решение не должно ходить в сеть.
                    "enable_network": False,
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


def _result(test: Case, data: dict) -> TestResult:
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
