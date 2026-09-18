"""Личные напоминания о дедлайне задания."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app import notify
from app.config import settings
from app.models import (
    Assignment,
    Group,
    GroupMembership,
    Platform,
    PlatformAccount,
    Problem,
    ProblemSet,
    ProblemSetItem,
    Role,
    Submission,
    User,
    utcnow,
)


@pytest.fixture
def outbox(monkeypatch):
    """Напоминание у каждого своё, поэтому перехватываем поимённую рассылку."""
    sent: list[tuple[str, str]] = []

    async def fake_send_each(messages, button=None):
        items = [(str(chat), text) for chat, text in messages]
        sent.extend(items)
        return len(items)

    monkeypatch.setattr(notify, "send_each", fake_send_each)
    monkeypatch.setattr(settings, "assignment_reminder_hours", 24)
    return sent


@pytest.fixture
async def world(session):
    """Группа из двоих: Аня решила одну задачу из двух, Боря ничего."""
    anya = User(display_name="Аня", role=Role.student, telegram_id=11)
    borya = User(display_name="Боря", role=Role.student, telegram_id=22)
    group = Group(title="Осень", join_code="REM123")
    problems = [
        Problem(platform=Platform.leetcode, external_id=str(i), slug=f"p{i}",
                title=f"Задача {i}", url="", difficulty="Easy")
        for i in (1, 2)
    ]
    problem_set = ProblemSet(title="Список")
    session.add_all([anya, borya, group, problem_set, *problems])
    await session.commit()

    session.add_all([
        GroupMembership(group_id=group.id, user_id=anya.id),
        GroupMembership(group_id=group.id, user_id=borya.id),
    ])
    session.add_all([
        ProblemSetItem(problem_set_id=problem_set.id, problem_id=p.id, position=i)
        for i, p in enumerate(problems)
    ])
    await session.commit()

    account = PlatformAccount(user_id=anya.id, platform=Platform.leetcode,
                              handle="anya", verified_at=utcnow() - timedelta(days=30))
    session.add(account)
    await session.commit()
    return {"anya": anya, "borya": borya, "group": group,
            "set": problem_set, "problems": problems, "account": account}


async def _assign(session, world, *, deadline, **kwargs) -> Assignment:
    item = Assignment(
        title="Неделя 3 — бинпоиск", problem_set_id=world["set"].id,
        group_id=world["group"].id, assigned_at=utcnow() - timedelta(days=3),
        deadline=deadline, **kwargs,
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item


async def _solve(session, world, problem, when):
    session.add(Submission(
        user_id=world["anya"].id, platform_account_id=world["account"].id,
        platform=Platform.leetcode, external_id=f"s{problem.id}", problem_id=problem.id,
        problem_slug=problem.slug, verdict="Accepted", is_accepted=True, submitted_at=when,
    ))
    await session.commit()


async def test_reminder_goes_only_to_those_who_have_not_finished(session, world, outbox):
    await _solve(session, world, world["problems"][0], utcnow() - timedelta(days=1))
    await _solve(session, world, world["problems"][1], utcnow() - timedelta(days=1))
    # Аня закрыла обе, Боря — ни одной.
    await _assign(session, world, deadline=utcnow() + timedelta(hours=5))

    assert await notify.send_deadline_reminders(session) == 1
    chat, text = outbox[0]
    assert chat == "22"                                   # только Боря
    assert "Осталось задач: 2 из 2" in text
    assert "Неделя 3" in text


async def test_reminder_counts_what_is_left_for_each(session, world, outbox):
    await _solve(session, world, world["problems"][0], utcnow() - timedelta(days=1))
    await _assign(session, world, deadline=utcnow() + timedelta(hours=3))

    assert await notify.send_deadline_reminders(session) == 2
    texts = dict(outbox)
    assert "Осталось задач: 1 из 2" in texts["11"]        # Аня доделывает одну
    assert "Осталось задач: 2 из 2" in texts["22"]


async def test_far_deadline_and_past_deadline_are_left_alone(session, world, outbox):
    await _assign(session, world, deadline=utcnow() + timedelta(days=5))
    await _assign(session, world, deadline=utcnow() - timedelta(hours=1))

    assert await notify.send_deadline_reminders(session) == 0
    assert outbox == []


async def test_reminder_is_sent_once(session, world, outbox):
    item = await _assign(session, world, deadline=utcnow() + timedelta(hours=6))

    assert await notify.send_deadline_reminders(session) == 2
    assert await notify.send_deadline_reminders(session) == 0
    await session.refresh(item)
    assert item.reminded_at is not None


async def test_reminder_says_the_deadline_is_final(session, world, outbox):
    """Напоминание должно называть цену опоздания, иначе оно просто шум."""
    await _assign(session, world, deadline=utcnow() + timedelta(hours=2))

    await notify.send_deadline_reminders(session)
    assert "не засчитываются" in outbox[0][1]


async def test_assignment_without_a_deadline_never_reminds(session, world, outbox):
    await _assign(session, world, deadline=None)

    assert await notify.send_deadline_reminders(session) == 0


async def test_students_without_telegram_are_skipped(session, world, outbox):
    world["borya"].telegram_id = None
    await session.commit()
    await _assign(session, world, deadline=utcnow() + timedelta(hours=4))

    assert await notify.send_deadline_reminders(session) == 1
    assert outbox[0][0] == "11"


def test_text_mentions_the_hours_left():
    item = Assignment(
        id=7, title="Неделя 3", problem_set_id=1,
        deadline=datetime.now(UTC) + timedelta(hours=21),
    )
    text = notify.deadline_reminder_text(item, left=3, total=5)
    assert "Через 21 час дедлайн" in text
    assert "Осталось задач: 3 из 5" in text
