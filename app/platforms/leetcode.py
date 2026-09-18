from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx

from app.config import settings
from app.i18n import translate as _
from app.platforms.base import (
    PlatformError,
    RateLimiter,
    RemoteProblem,
    RemoteProfile,
    RemoteSubmission,
    UserNotFound,
)

GRAPHQL = "https://leetcode.com/graphql/"

PROBLEM_LIST_QUERY = """
query problemsetQuestionList($categorySlug: String!, $limit: Int, $skip: Int,
                             $filters: QuestionListFilterInput) {
  questionList(categorySlug: $categorySlug, limit: $limit, skip: $skip, filters: $filters) {
    total: totalNum
    questions: data {
      questionFrontendId
      title
      titleSlug
      difficulty
      isPaidOnly
      topicTags { name slug }
    }
  }
}
"""

RECENT_AC_QUERY = """
query recentAcSubmissions($username: String!, $limit: Int!) {
  recentAcSubmissionList(username: $username, limit: $limit) {
    id
    title
    titleSlug
    timestamp
  }
}
"""

PROFILE_QUERY = """
query userProfile($username: String!) {
  matchedUser(username: $username) {
    username
    githubUrl
    profile { realName aboutMe company school websites countryName }
  }
}
"""


class LeetCodeClient:
    """Неофициальный публичный GraphQL LeetCode. Без кук и токенов.

    Доступно: каталог задач, профиль и последние ~20 ПРИНЯТЫХ решений.
    Недоступно: неудачные попытки и история глубже этих 20 решений —
    для них нужна сессионная кука аккаунта, которую мы намеренно не просим.
    """

    MAX_RECENT = 20

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._owns_client = client is None
        self._limiter = RateLimiter(settings.leetcode_min_interval)

    async def __aenter__(self) -> LeetCodeClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=settings.http_timeout)
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "User-Agent": settings.leetcode_user_agent,
            "Referer": "https://leetcode.com",
            "Origin": "https://leetcode.com",
        }

    async def _query(self, query: str, variables: dict, *, retries: int = 2) -> dict:
        if self._client is None:
            raise RuntimeError("LeetCodeClient используется вне async with")

        last_error: Exception | None = None
        for attempt in range(retries + 1):
            async with self._limiter:
                try:
                    response = await self._client.post(
                        GRAPHQL,
                        json={"query": query, "variables": variables},
                        headers=self._headers,
                    )
                except httpx.HTTPError as exc:
                    last_error = PlatformError(_("LeetCode недоступен: %(why)s") % {"why": exc})
                    response = None

            if response is not None:
                if response.status_code in (429, 403) or response.status_code >= 500:
                    last_error = PlatformError(
                        _("LeetCode вернул %(code)s") % {"code": response.status_code}
                    )
                else:
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        raise PlatformError(_("LeetCode вернул не JSON")) from exc
                    if payload.get("errors"):
                        message = payload["errors"][0].get("message", _("ошибка GraphQL"))
                        raise PlatformError(f"LeetCode: {message}")
                    return payload.get("data") or {}

            if attempt < retries:
                await asyncio.sleep(2 ** attempt)

        raise last_error or PlatformError(_("LeetCode: не удалось выполнить запрос"))

    async def fetch_problems(self, page_size: int = 100) -> list[RemoteProblem]:
        out: list[RemoteProblem] = []
        skip = 0
        total = None
        while total is None or skip < total:
            data = await self._query(
                PROBLEM_LIST_QUERY,
                {"categorySlug": "", "limit": page_size, "skip": skip, "filters": {}},
            )
            block = data.get("questionList") or {}
            total = block.get("total", 0)
            questions = block.get("questions") or []
            if not questions:
                break
            for q in questions:
                slug = q["titleSlug"]
                out.append(
                    RemoteProblem(
                        external_id=q["questionFrontendId"],
                        slug=slug,
                        title=q["title"],
                        url=f"https://leetcode.com/problems/{slug}/",
                        difficulty=q.get("difficulty"),
                        tags=[t["name"] for t in (q.get("topicTags") or [])],
                        is_paid_only=bool(q.get("isPaidOnly")),
                    )
                )
            skip += page_size
        return out

    async def fetch_profile(self, username: str) -> RemoteProfile:
        data = await self._query(PROFILE_QUERY, {"username": username})
        matched = data.get("matchedUser")
        if not matched:
            raise UserNotFound(username)
        profile = matched.get("profile") or {}
        fields = [
            profile.get("realName"),
            profile.get("aboutMe"),
            profile.get("company"),
            profile.get("school"),
            matched.get("githubUrl"),
            *(profile.get("websites") or []),
        ]
        return RemoteProfile(
            handle=matched.get("username", username),
            display_name=profile.get("realName") or matched.get("username"),
            searchable_text=" ".join(x for x in fields if x),
        )

    async def fetch_submissions(
        self, username: str, limit: int | None = None
    ) -> list[RemoteSubmission]:
        limit = min(limit or self.MAX_RECENT, self.MAX_RECENT)
        data = await self._query(RECENT_AC_QUERY, {"username": username, "limit": limit})
        items = data.get("recentAcSubmissionList")
        if items is None:
            raise UserNotFound(username)
        out: list[RemoteSubmission] = []
        for item in items:
            slug = item.get("titleSlug")
            out.append(
                RemoteSubmission(
                    external_id=str(item["id"]),
                    problem_external_id=None,  # в этом ответе номера задачи нет, матчим по slug
                    problem_slug=slug,
                    problem_title=item.get("title"),
                    verdict="Accepted",
                    is_accepted=True,
                    submitted_at=datetime.fromtimestamp(int(item["timestamp"]), tz=UTC),
                    language=None,
                )
            )
        return out
