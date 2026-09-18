from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.models import Platform, PlatformAccount, Problem, Submission, User
from app.platforms.base import PlatformError, RemoteSubmission
from app.services import sync as sync_service

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


class FakeClient:
    """Подменяет клиент платформы: отдаёт заготовленные посылки."""

    def __init__(self, submissions, error: Exception | None = None) -> None:
        self._submissions = submissions
        self._error = error
        self.calls: list[tuple] = []

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return None

    async def fetch_submissions(self, handle, count=None, offset=1, limit=None):
        self.calls.append((handle, count, offset))
        if self._error:
            raise self._error
        return self._submissions


@pytest.fixture
async def account(session):
    user = User(display_name="Аня")
    session.add(user)
    await session.commit()
    session.add(
        Problem(platform=Platform.leetcode, external_id="1", slug="two-sum",
                title="Two Sum", url="https://leetcode.com/problems/two-sum/",
                difficulty="Easy")
    )
    acc = PlatformAccount(user_id=user.id, platform=Platform.leetcode,
                          handle="anya", verified_at=NOW)
    session.add(acc)
    await session.commit()
    return acc


def _sub(external_id: str, slug: str | None, when: datetime) -> RemoteSubmission:
    return RemoteSubmission(
        external_id=external_id, problem_external_id=None, problem_slug=slug,
        problem_title="Two Sum", verdict="Accepted", is_accepted=True, submitted_at=when,
    )


async def test_sync_links_submissions_to_catalog(session, account, monkeypatch):
    fake = FakeClient([_sub("a1", "two-sum", NOW)])
    monkeypatch.setattr(sync_service, "LeetCodeClient", fake)

    added = await sync_service.sync_account(session, account)
    assert added == 1

    submission = await session.scalar(select(Submission))
    assert submission.problem_id is not None
    assert submission.user_id == account.user_id
    assert account.last_synced_at is not None
    assert account.last_sync_error is None


async def test_sync_is_idempotent(session, account, monkeypatch):
    fake = FakeClient([_sub("a1", "two-sum", NOW)])
    monkeypatch.setattr(sync_service, "LeetCodeClient", fake)

    assert await sync_service.sync_account(session, account) == 1
    assert await sync_service.sync_account(session, account) == 0
    assert await session.scalar(select(func.count()).select_from(Submission)) == 1


async def test_unknown_problem_stays_unlinked_then_relinks(session, account, monkeypatch):
    fake = FakeClient([_sub("a1", "свежая-задача-с-контеста", NOW)])
    monkeypatch.setattr(sync_service, "LeetCodeClient", fake)
    await sync_service.sync_account(session, account)

    submission = await session.scalar(select(Submission))
    assert submission.problem_id is None

    # Каталог обновился — задача появилась.
    session.add(
        Problem(platform=Platform.leetcode, external_id="9999",
                slug="свежая-задача-с-контеста", title="Fresh",
                url="https://leetcode.com/problems/fresh/", difficulty="Hard")
    )
    await session.commit()

    assert await sync_service.relink_orphan_submissions(session) == 1
    await session.refresh(submission)
    assert submission.problem_id is not None


async def test_platform_error_is_recorded_not_raised(session, account, monkeypatch):
    fake = FakeClient([], error=PlatformError("LeetCode вернул 429"))
    monkeypatch.setattr(sync_service, "LeetCodeClient", fake)

    assert await sync_service.sync_account(session, account) == 0
    assert "429" in account.last_sync_error
    # Не отмечаем как успешно синхронизированный — попробуем на следующем круге.
    assert account.last_synced_at is None


async def test_unverified_account_is_skipped(session, account, monkeypatch):
    account.verified_at = None
    await session.commit()
    fake = FakeClient([_sub("a1", "two-sum", NOW)])
    monkeypatch.setattr(sync_service, "LeetCodeClient", fake)

    assert await sync_service.sync_account(session, account) == 0
    assert await session.scalar(select(func.count()).select_from(Submission)) == 0


async def test_due_for_sync_respects_staleness(session, account, monkeypatch):
    from app.config import settings
    from app.models import utcnow

    due = await sync_service.accounts_due_for_sync(session)
    assert [a.id for a in due] == [account.id]  # ни разу не синхронизировали

    account.last_synced_at = utcnow()
    await session.commit()
    assert await sync_service.accounts_due_for_sync(session) == []

    account.last_synced_at = utcnow() - timedelta(seconds=settings.sync_stale_seconds + 60)
    await session.commit()
    assert len(await sync_service.accounts_due_for_sync(session)) == 1


async def test_codeforces_first_sync_backfills_deeper(session, monkeypatch):
    user = User(display_name="Боря")
    session.add(user)
    await session.commit()
    acc = PlatformAccount(user_id=user.id, platform=Platform.codeforces,
                          handle="borya", verified_at=NOW)
    session.add(acc)
    await session.commit()

    fake = FakeClient([])  # пустая страница обрывает бэкфилл на первом запросе
    monkeypatch.setattr(sync_service, "CodeforcesClient", fake)
    await sync_service.sync_account(session, acc)
    assert fake.calls == [("borya", sync_service.CF_BACKFILL_PAGE, 1)]

    fake.calls.clear()
    await sync_service.sync_account(session, acc)
    assert fake.calls == [("borya", sync_service.CF_INCREMENTAL, 1)]


async def test_group_sync_updates_every_student(session, client, monkeypatch):
    """Кнопка «обновить группу»: обход профилей по одному — не работа для человека."""
    from app.models import Group, GroupMembership, Role
    from app.platforms import base as platforms_base  # noqa: F401

    teacher = User(display_name="Кирилл", role=Role.teacher)
    anya, borya = User(display_name="Аня"), User(display_name="Боря")
    session.add_all([teacher, anya, borya])
    await session.commit()

    group = Group(title="Осень", join_code="AAA111")
    session.add(group)
    await session.commit()
    session.add_all([
        GroupMembership(group_id=group.id, user_id=anya.id),
        GroupMembership(group_id=group.id, user_id=borya.id),
    ])
    session.add_all([
        PlatformAccount(user_id=anya.id, platform=Platform.leetcode,
                        handle="anya", verified_at=NOW),
        PlatformAccount(user_id=borya.id, platform=Platform.leetcode,
                        handle="borya", verified_at=NOW),
    ])
    session.add(Problem(platform=Platform.leetcode, external_id="1", slug="two-sum",
                        title="Two Sum", url="", difficulty="Easy"))
    await session.commit()

    class PerHandle(FakeClient):
        """Номер посылки уникален на платформе, поэтому он зависит от студента."""

        async def fetch_submissions(self, handle, count=None, offset=1, limit=None):
            self.calls.append((handle, count, offset))
            return [
                RemoteSubmission(
                    external_id=f"s-{handle}", problem_external_id=None,
                    problem_slug="two-sum", problem_title="Two Sum",
                    verdict="Accepted", is_accepted=True, submitted_at=NOW,
                )
            ]

    fake = PerHandle([])
    monkeypatch.setattr(sync_service, "LeetCodeClient", fake)

    await client.post("/login/dev", data={"name": "Кирилл", "teacher": "true"})
    response = await client.post(f"/teacher/groups/{group.id}/sync")
    assert "Обновляю результаты: 2 студентов" in response.text

    # Фоновая задача выполняется после ответа — к этому моменту посылки уже есть.
    submissions = await session.scalar(select(func.count()).select_from(Submission))
    assert submissions == 2
    assert {call[0] for call in fake.calls} == {"anya", "borya"}
