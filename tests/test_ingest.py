"""Приём исходников посылок: кто может класть код и что с ним потом."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.models import Platform, PlatformAccount, Problem, Role, Submission, User

BASE = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def token(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "ingest_token", "test-token")
    return "test-token"


@pytest.fixture
async def submission(session):
    user = User(display_name="Аня")
    session.add(user)
    await session.commit()
    problem = Problem(platform=Platform.codeforces, external_id="4A", slug="4a",
                      title="Watermelon", url="https://codeforces.com/problemset/problem/4/A")
    account = PlatformAccount(user_id=user.id, platform=Platform.codeforces,
                              handle="anya", verified_at=BASE)
    session.add_all([problem, account])
    await session.commit()
    row = Submission(user_id=user.id, platform_account_id=account.id,
                     platform=Platform.codeforces, external_id="777", problem_id=problem.id,
                     problem_slug="4a", verdict="OK", is_accepted=True, submitted_at=BASE)
    session.add(row)
    await session.commit()
    return row


async def test_script_asks_what_to_fetch(session, client, submission):
    response = await client.get("/ingest/wanted", headers={"X-Ingest-Token": "test-token"})
    assert response.status_code == 200
    assert response.json() == [
        {"platform": "codeforces", "external_id": "777", "problem_slug": "4a"}
    ]


async def test_code_is_stored_and_shown(session, client, submission):
    put = await client.post(
        "/ingest/source",
        headers={"X-Ingest-Token": "test-token"},
        json={"platform": "codeforces", "external_id": "777", "code": "print('hi')\n"},
    )
    assert put.status_code == 200
    await session.refresh(submission)
    assert submission.code == "print('hi')\n" and submission.code_fetched_at is not None

    # Больше не просим этот исходник.
    again = await client.get("/ingest/wanted", headers={"X-Ingest-Token": "test-token"})
    assert again.json() == []

    await client.post("/login/dev", data={"name": "Кирилл", "teacher": "true"})
    page = await client.get(f"/submissions/{submission.id}")
    assert page.status_code == 200
    assert "print" in page.text


async def test_wrong_token_is_refused(session, client, submission):
    wrong = await client.get("/ingest/wanted", headers={"X-Ingest-Token": "wrong"})
    assert wrong.status_code == 403
    assert (await client.get("/ingest/wanted")).status_code == 403


async def test_ingest_is_off_without_a_token(session, client, submission, monkeypatch):
    """Пустой токен выключает ручку совсем: лучше её не иметь, чем держать открытой."""
    from app.config import settings

    monkeypatch.setattr(settings, "ingest_token", "")
    assert (await client.get("/ingest/wanted", headers={"X-Ingest-Token": ""})).status_code == 404


async def test_student_cannot_read_a_foreign_submission(session, client, submission):
    submission.code = "секрет"
    await session.commit()

    borya = User(display_name="Боря", role=Role.student)
    session.add(borya)
    await session.commit()
    await client.post("/login/dev", data={"name": "Боря"})

    response = await client.get(f"/submissions/{submission.id}")
    assert "секрет" not in response.text
