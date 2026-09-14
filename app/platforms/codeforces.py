from __future__ import annotations

import re
from datetime import UTC, datetime

import httpx

from app.config import settings
from app.platforms.base import (
    PlatformError,
    RateLimiter,
    RemoteProblem,
    RemoteProfile,
    RemoteSubmission,
    UserNotFound,
)

API = "https://codeforces.com/api"


def _problem_external_id(problem: dict) -> str | None:
    index = problem.get("index")
    if not index:
        return None
    contest_id = problem.get("contestId")
    if contest_id is not None:
        return f"{contest_id}{index}"
    # Задачи из архива acmsguru приходят без contestId.
    name = problem.get("problemsetName")
    return f"{name}:{index}" if name else None


def submission_url(problem_slug: str | None, submission_id: str) -> str | None:
    """Публичная страница посылки: код видно всем, авторизация не нужна.

    Номер контеста берём из слага задачи («1234a» → 1234). У задач из архива
    acmsguru контеста нет, и страницы посылки в таком виде тоже — тогда None.
    """
    match = re.match(r"^(\d+)", problem_slug or "")
    if not match:
        return None
    return f"https://codeforces.com/contest/{match.group(1)}/submission/{submission_id}"


def _problem_url(problem: dict) -> str:
    contest_id = problem.get("contestId")
    index = problem.get("index", "")
    if contest_id is not None:
        return f"https://codeforces.com/problemset/problem/{contest_id}/{index}"
    return f"https://codeforces.com/problemsets/acmsguru/problem/99999/{index}"


class CodeforcesClient:
    """Официальный публичный API Codeforces. Авторизация не требуется."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._owns_client = client is None
        self._limiter = RateLimiter(settings.codeforces_min_interval)

    async def __aenter__(self) -> CodeforcesClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=settings.http_timeout)
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get(self, method: str, **params) -> list | dict:
        if self._client is None:
            raise RuntimeError("CodeforcesClient используется вне async with")
        async with self._limiter:
            try:
                response = await self._client.get(f"{API}/{method}", params=params)
            except httpx.HTTPError as exc:
                raise PlatformError(f"Codeforces недоступен: {exc}") from exc

        if response.status_code >= 500:
            raise PlatformError(f"Codeforces вернул {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise PlatformError("Codeforces вернул не JSON") from exc

        if payload.get("status") != "OK":
            comment = payload.get("comment", "")
            if "not found" in comment.lower():
                raise UserNotFound(comment)
            raise PlatformError(f"Codeforces: {comment or 'неизвестная ошибка'}")
        return payload["result"]

    async def fetch_problems(self) -> list[RemoteProblem]:
        result = await self._get("problemset.problems")
        out: list[RemoteProblem] = []
        for problem in result.get("problems", []):
            external_id = _problem_external_id(problem)
            if not external_id:
                continue
            out.append(
                RemoteProblem(
                    external_id=external_id,
                    slug=external_id.lower(),
                    title=problem.get("name") or external_id,
                    url=_problem_url(problem),
                    rating=problem.get("rating"),
                    tags=list(problem.get("tags") or []),
                )
            )
        return out

    async def fetch_profile(self, handle: str) -> RemoteProfile:
        result = await self._get("user.info", handles=handle)
        if not result:
            raise UserNotFound(handle)
        info = result[0]
        # Студент может вписать код верификации в любое из этих полей.
        fields = [
            info.get("firstName"),
            info.get("lastName"),
            info.get("organization"),
            info.get("city"),
        ]
        name = " ".join(x for x in (info.get("firstName"), info.get("lastName")) if x)
        return RemoteProfile(
            handle=info.get("handle", handle),
            display_name=name or info.get("handle"),
            searchable_text=" ".join(x for x in fields if x),
        )

    async def fetch_submissions(
        self, handle: str, count: int = 300, offset: int = 1
    ) -> list[RemoteSubmission]:
        """Посылки, самые свежие первыми. `offset` — 1-based, как требует API."""
        result = await self._get(
            "user.status", handle=handle, **{"from": offset, "count": count}
        )
        out: list[RemoteSubmission] = []
        for item in result:
            problem = item.get("problem") or {}
            external_id = _problem_external_id(problem)
            verdict = item.get("verdict")
            out.append(
                RemoteSubmission(
                    external_id=str(item["id"]),
                    problem_external_id=external_id,
                    problem_slug=external_id.lower() if external_id else None,
                    problem_title=problem.get("name"),
                    verdict=verdict,
                    is_accepted=verdict == "OK",
                    submitted_at=datetime.fromtimestamp(item["creationTimeSeconds"], tz=UTC),
                    language=item.get("programmingLanguage"),
                )
            )
        return out
