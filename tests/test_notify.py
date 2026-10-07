from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app import notify
from app.config import settings
from app.models import Announcement, Assignment, Group, GroupMembership, ProblemSet, User


@pytest.fixture
def outbox(monkeypatch):
    sent: list[tuple[str, str]] = []

    async def fake_send_many(chat_ids, text, button=None):
        ids = [str(c) for c in chat_ids]
        sent.extend((c, text) for c in ids)
        return len(ids)

    monkeypatch.setattr(notify, "send_many", fake_send_many)
    monkeypatch.setattr(settings, "telegram_notify_chat_id", "-100777")
    return sent


def test_announcement_text_has_title_time_and_button():
    item = Announcement(
        title="Раунд <999>", body="Сбор в 17:45",
        url="https://cf.example/1", url_label="Регистрация",
        starts_at=datetime(2026, 9, 15, 16, 51, tzinfo=UTC),
        ends_at=datetime(2026, 9, 15, 18, 51, tzinfo=UTC),
    )
    text = notify.announcement_text(item)
    assert "<b>Раунд &lt;999&gt;</b>" in text  # HTML экранирован
    assert "15.09.2026 19:51 — 21:51" in text  # московское время
    # Ссылка теперь не в тексте, а кнопкой под сообщением.
    assert notify.announcement_button(item) == ("Регистрация", "https://cf.example/1")


async def test_announcement_goes_to_group_chat_when_set(session, outbox):
    group = Group(title="А", join_code="AAA111", telegram_chat_id="-100555")
    session.add(group)
    await session.commit()
    item = Announcement(title="Только группе", group_id=group.id)
    session.add(item)
    await session.commit()
    await session.refresh(item)

    assert await notify.notify_announcement(item, session) == 1
    assert outbox[0][0] == "-100555"


async def test_announcement_falls_back_to_club_chat(session, outbox):
    item = Announcement(title="Всем")
    session.add(item)
    await session.commit()
    await session.refresh(item)
    assert await notify.notify_announcement(item) == 1
    assert outbox[0][0] == "-100777"


async def test_nothing_sent_when_there_is_nobody_to_write_to(session, outbox, monkeypatch):
    monkeypatch.setattr(settings, "telegram_notify_chat_id", "")
    item = Announcement(title="Тихо")
    session.add(item)
    await session.commit()
    await session.refresh(item)
    assert await notify.notify_announcement(item, session) == 0
    assert outbox == []


async def test_without_chat_bot_writes_to_everyone_personally(session, outbox, monkeypatch):
    monkeypatch.setattr(settings, "telegram_notify_chat_id", "")
    session.add_all([
        User(display_name="Аня", telegram_id=11),
        User(display_name="Боря", telegram_id=22),
        User(display_name="Вика"),  # входил только через Authentik — писать некуда
        User(display_name="Гена", telegram_id=44, is_active=False),
    ])
    item = Announcement(title="Всем лично")
    session.add(item)
    await session.commit()
    await session.refresh(item)

    assert await notify.notify_announcement(item, session) == 2
    assert {chat for chat, _ in outbox} == {"11", "22"}


async def test_group_announcement_reaches_only_its_members(session, outbox, monkeypatch):
    monkeypatch.setattr(settings, "telegram_notify_chat_id", "")
    group = Group(title="А", join_code="AAA111")
    inside = User(display_name="Свой", telegram_id=11)
    outside = User(display_name="Чужой", telegram_id=22)
    session.add_all([group, inside, outside])
    await session.commit()
    session.add(GroupMembership(group_id=group.id, user_id=inside.id))
    item = Announcement(title="Только группе", group_id=group.id)
    session.add(item)
    await session.commit()
    await session.refresh(item)

    assert await notify.notify_announcement(item, session) == 1
    assert outbox[0][0] == "11"


async def test_personal_assignment_never_goes_to_the_club_chat(session, outbox):
    student = User(display_name="Один", telegram_id=11)
    other = User(display_name="Другой", telegram_id=22)
    problem_set = ProblemSet(title="Набор")
    session.add_all([student, other, problem_set])
    await session.commit()
    item = Assignment(title="Лично", problem_set_id=problem_set.id, user_id=student.id)
    session.add(item)
    await session.commit()
    await session.refresh(item)

    assert await notify.notify_assignment(item, 3, session) == 1
    assert outbox[0][0] == "11"


async def test_reminders_only_inside_window_and_once(session, outbox, monkeypatch):
    monkeypatch.setattr(settings, "reminder_minutes_before", 60)
    now = datetime.now(UTC)
    session.add_all([
        Announcement(title="Скоро", starts_at=now + timedelta(minutes=30)),
        Announcement(title="Позже", starts_at=now + timedelta(hours=5)),
        Announcement(title="Уже идёт", starts_at=now - timedelta(minutes=5)),
        Announcement(title="Уже напомнили", starts_at=now + timedelta(minutes=20), reminded_at=now),
    ])
    await session.commit()

    assert await notify.send_due_reminders(session) == 1
    assert "Скоро" in outbox[0][1] and "Через" in outbox[0][1]

    # Второй круг — ничего нового.
    assert await notify.send_due_reminders(session) == 0
    item = await session.scalar(select(Announcement).where(Announcement.title == "Скоро"))
    assert item.reminded_at is not None


async def test_publishing_announcement_notifies(session, client, outbox):
    await client.post("/login/dev", data={"name": "Кирилл", "teacher": "true"})
    response = await client.post("/teacher/announcements", data={"title": "Тест", "url": ""})
    assert "отправлено в Telegram" in response.text
    assert len(outbox) == 1 and "Тест" in outbox[0][1]


async def test_assignment_text_and_route(session, client, outbox):
    await client.post("/login/dev", data={"name": "Кирилл", "teacher": "true"})
    await client.post("/teacher/groups", data={"title": "Группа"})
    group = await session.scalar(select(Group))
    problem_set = ProblemSet(title="Список")
    session.add(problem_set)
    await session.commit()

    await client.post("/teacher/assignments", data={
        "title": "Неделя 1", "problem_set_id": problem_set.id, "group_id": group.id,
        # Дата далеко впереди: задание начинается «сейчас», и срок в прошлом
        # маршрут отклоняет — тест не должен истекать вместе с календарём.
        "deadline": "2030-10-01T18:00",
    })
    assert len(outbox) == 1
    text = outbox[0][1]
    assert "Неделя 1" in text and "Дедлайн: 01.10.2030 18:00" in text
    # Ссылка на задание ушла в кнопку под сообщением.
    assignment = await session.scalar(select(Assignment))
    assert notify.assignment_button(assignment)[1].endswith(f"/assignments/{assignment.id}")


async def test_group_chat_id_validation(session, client):
    await client.post("/login/dev", data={"name": "Кирилл", "teacher": "true"})
    await client.post("/teacher/groups", data={"title": "Группа"})
    group = await session.scalar(select(Group))

    bad = await client.post(f"/teacher/groups/{group.id}/chat", data={"chat_id": "abc"})
    assert "число" in bad.text
    ok = await client.post(f"/teacher/groups/{group.id}/chat", data={"chat_id": "-1001234"})
    assert "сохранён" in ok.text
    await session.refresh(group)
    assert group.telegram_chat_id == "-1001234"
