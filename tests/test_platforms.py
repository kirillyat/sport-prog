"""Клиенты платформ на зафиксированных ответах.

Формы ответов сняты с живых API — если платформа сменит схему,
эти тесты не поймают, но регрессии в нашем разборе поймают.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from app.platforms import CodeforcesClient, LeetCodeClient
from app.platforms.base import PlatformError, UserNotFound

CF_PROBLEMS = {
    "status": "OK",
    "result": {
        "problems": [
            {"contestId": 4, "index": "A", "name": "Watermelon", "type": "PROGRAMMING",
             "rating": 800, "tags": ["brute force", "math"]},
            {"contestId": 2263, "index": "B", "name": "Min Matrices", "type": "PROGRAMMING",
             "points": 1000.0, "tags": ["constructive algorithms"]},
            {"index": "125", "problemsetName": "acmsguru", "name": "Shope", "tags": []},
        ],
        "problemStatistics": [],
    },
}

CF_STATUS = {
    "status": "OK",
    "result": [
        {"id": 390464037, "contestId": 2262, "creationTimeSeconds": 1789228631,
         "problem": {"contestId": 2262, "index": "C", "name": "Traveling the World", "tags": []},
         "programmingLanguage": "C++23", "verdict": "OK"},
        {"id": 390464000, "contestId": 2262, "creationTimeSeconds": 1789228000,
         "problem": {"contestId": 2262, "index": "C", "name": "Traveling the World", "tags": []},
         "programmingLanguage": "C++23", "verdict": "WRONG_ANSWER"},
    ],
}

CF_USER = {
    "status": "OK",
    "result": [{"handle": "student", "firstName": "Иван", "lastName": "Петров",
                "organization": "msu-sport-deadbeef", "rating": 1400}],
}

LC_PROBLEMS = {
    "data": {"questionList": {"total": 2, "questions": [
        {"questionFrontendId": "1", "title": "Two Sum", "titleSlug": "two-sum",
         "difficulty": "Easy", "isPaidOnly": False,
         "topicTags": [{"name": "Array", "slug": "array"}]},
        {"questionFrontendId": "2", "title": "Add Two Numbers", "titleSlug": "add-two-numbers",
         "difficulty": "Medium", "isPaidOnly": False, "topicTags": []},
    ]}}
}

LC_RECENT = {
    "data": {"recentAcSubmissionList": [
        {"id": "2140228077", "title": "Two Sum", "titleSlug": "two-sum",
         "timestamp": "1789272451"},
    ]}
}

LC_PROFILE = {
    "data": {"matchedUser": {"username": "anya", "githubUrl": None, "profile": {
        "realName": "Anya", "aboutMe": "msu-sport-cafe1234", "company": None,
        "school": None, "websites": [], "countryName": "Russia"}}}
}


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_codeforces_problems_include_acmsguru():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=CF_PROBLEMS)

    async with CodeforcesClient(_client(handler)) as client:
        problems = await client.fetch_problems()

    ids = [p.external_id for p in problems]
    assert ids == ["4A", "2263B", "acmsguru:125"]
    assert problems[0].rating == 800
    assert problems[1].rating is None  # у задачи может не быть рейтинга
    assert problems[0].url == "https://codeforces.com/problemset/problem/4/A"


async def test_codeforces_submissions_verdicts():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["handle"] == "student"
        return httpx.Response(200, json=CF_STATUS)

    async with CodeforcesClient(_client(handler)) as client:
        subs = await client.fetch_submissions("student")

    assert [s.is_accepted for s in subs] == [True, False]
    assert subs[0].problem_external_id == "2262C"
    assert subs[0].submitted_at == datetime.fromtimestamp(1789228631, tz=UTC)


async def test_codeforces_profile_exposes_verification_fields():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=CF_USER)

    async with CodeforcesClient(_client(handler)) as client:
        profile = await client.fetch_profile("student")

    assert "msu-sport-deadbeef" in profile.searchable_text
    assert profile.display_name == "Иван Петров"


async def test_codeforces_unknown_handle():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"status": "FAILED",
                                         "comment": "handles: User with handle xxx not found"})

    async with CodeforcesClient(_client(handler)) as client:
        with pytest.raises(UserNotFound):
            await client.fetch_profile("xxx")


async def test_leetcode_problem_catalog_paginates():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        # Вторая страница пустая — на ней пагинация должна остановиться.
        if calls["n"] > 1:
            empty = {"data": {"questionList": {"total": 2, "questions": []}}}
            return httpx.Response(200, json=empty)
        return httpx.Response(200, json=LC_PROBLEMS)

    async with LeetCodeClient(_client(handler)) as client:
        problems = await client.fetch_problems(page_size=2)

    assert [p.slug for p in problems] == ["two-sum", "add-two-numbers"]
    assert problems[0].url == "https://leetcode.com/problems/two-sum/"
    assert calls["n"] == 1  # total=2 достигнут, второй запрос не нужен


async def test_leetcode_recent_accepted():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=LC_RECENT)

    async with LeetCodeClient(_client(handler)) as client:
        subs = await client.fetch_submissions("anya")

    assert len(subs) == 1
    assert subs[0].is_accepted is True
    assert subs[0].problem_slug == "two-sum"
    assert subs[0].submitted_at == datetime.fromtimestamp(1789272451, tz=UTC)


async def test_leetcode_missing_user_is_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"recentAcSubmissionList": None}})

    async with LeetCodeClient(_client(handler)) as client:
        with pytest.raises(UserNotFound):
            await client.fetch_submissions("нет-такого")


async def test_leetcode_profile_searchable_text():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=LC_PROFILE)

    async with LeetCodeClient(_client(handler)) as client:
        profile = await client.fetch_profile("anya")

    assert "msu-sport-cafe1234" in profile.searchable_text


async def test_leetcode_retries_then_fails():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, json={})

    async with LeetCodeClient(_client(handler)) as client:
        with pytest.raises(PlatformError):
            await client.fetch_profile("anya")

    assert calls["n"] == 3  # первая попытка + два ретрая


async def test_limiter_spaces_requests_across_clients(monkeypatch):
    """Клиент заводится на каждый аккаунт, а лимит у площадки один.

    Раньше лимитер жил внутри клиента и начинал отсчёт с нуля, поэтому синк
    группы уходил на Codeforces пачкой запросов и получал «Call limit exceeded».
    """
    import time

    from app.config import settings
    from app.platforms import CodeforcesClient

    monkeypatch.setattr(settings, "codeforces_min_interval", 0.05)
    start = time.monotonic()
    for _ in range(4):
        async with CodeforcesClient(_client(lambda r: httpx.Response(200, json={}))) as client:
            async with client._limiter:
                pass
    spent = time.monotonic() - start
    # Первый запрос идёт сразу, три следующих ждут свою паузу.
    assert spent >= 0.05 * 3
