from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import Announcement, Group, GroupMembership, Role, User, utcnow
from app.routers.announcements import list_visible, split_by_state, upcoming_for_dashboard

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


async def _login(client, name, teacher=False):
    data = {"name": name}
    if teacher:
        data["teacher"] = "true"
    await client.post("/login/dev", data=data)


@pytest.fixture
async def world(session):
    teacher = User(display_name="Кирилл", role=Role.teacher)
    anya = User(display_name="Аня")
    vova = User(display_name="Вова")
    session.add_all([teacher, anya, vova])
    await session.commit()

    group = Group(title="Группа А", join_code="AAA111")
    other = Group(title="Группа Б", join_code="BBB222")
    session.add_all([group, other])
    await session.commit()
    session.add_all([
        GroupMembership(group_id=group.id, user_id=anya.id),
        GroupMembership(group_id=other.id, user_id=vova.id),
    ])
    await session.commit()
    return {"teacher": teacher, "anya": anya, "vova": vova, "group": group, "other": other}


def test_state_upcoming_live_and_past():
    item = Announcement(title="Раунд", starts_at=NOW, ends_at=NOW + timedelta(hours=2))
    assert item.state(NOW - timedelta(hours=1)) == "upcoming"
    assert item.state(NOW + timedelta(minutes=30)) == "live"
    assert item.state(NOW + timedelta(hours=3)) == "past"


def test_state_without_times_is_plain():
    assert Announcement(title="Новость").state(NOW) == "plain"


def test_open_ended_event_goes_stale_after_a_day():
    item = Announcement(title="Раунд", starts_at=NOW)
    assert item.state(NOW + timedelta(hours=5)) == "live"
    assert item.state(NOW + timedelta(days=2)) == "past"


async def test_group_announcement_hidden_from_other_groups(session, world):
    session.add_all([
        Announcement(title="Для всех", group_id=None),
        Announcement(title="Только группе А", group_id=world["group"].id),
    ])
    await session.commit()

    anya = [a.title for a in await list_visible(session, world["anya"])]
    assert set(anya) == {"Для всех", "Только группе А"}

    vova = [a.title for a in await list_visible(session, world["vova"])]
    assert vova == ["Для всех"]

    teacher = [a.title for a in await list_visible(session, world["teacher"])]
    assert set(teacher) == {"Для всех", "Только группе А"}


async def test_pinned_comes_first(session, world):
    session.add_all([
        Announcement(title="Обычное", created_at=NOW + timedelta(hours=1)),
        Announcement(title="Закреплённое", pinned=True, created_at=NOW),
    ])
    await session.commit()
    titles = [a.title for a in await list_visible(session, world["anya"])]
    assert titles[0] == "Закреплённое"


async def test_upcoming_sorted_by_start(session, world):
    now = datetime.now(UTC)
    session.add_all([
        Announcement(title="Позже", starts_at=now + timedelta(days=5)),
        Announcement(title="Раньше", starts_at=now + timedelta(days=1)),
    ])
    await session.commit()
    grouped = split_by_state(await list_visible(session, world["anya"]))
    assert [a.title for a in grouped["upcoming"]] == ["Раньше", "Позже"]


async def test_dashboard_shows_live_before_upcoming(session, world):
    now = datetime.now(UTC)
    session.add_all([
        Announcement(title="Скоро", starts_at=now + timedelta(days=1)),
        Announcement(title="Идёт", starts_at=now - timedelta(hours=1),
                     ends_at=now + timedelta(hours=1)),
        Announcement(title="Прошло", starts_at=now - timedelta(days=9),
                     ends_at=now - timedelta(days=9) + timedelta(hours=2)),
    ])
    await session.commit()
    titles = [a.title for a in await upcoming_for_dashboard(session, world["anya"])]
    assert titles == ["Идёт", "Скоро"]


async def test_teacher_publishes_announcement_with_link(session, client):
    await client.post("/login/dev", data={"name": "Кирилл", "teacher": "true"})
    response = await client.post("/teacher/announcements", data={
        "title": "Codeforces Round 999",
        "body": "Сбор в 17:45",
        "url": "https://codeforces.com/contests/1234",
        "url_label": "Зарегистрироваться",
        "starts_at": "2026-12-01T18:00",
        "ends_at": "2026-12-01T20:00",
        "group_id": "",
        "pinned": "true",
    })
    assert "Объявление опубликовано" in response.text

    item = await session.scalar(select(Announcement))
    assert item.url == "https://codeforces.com/contests/1234"
    assert item.pinned is True
    assert item.starts_at is not None

    page = await client.get("/announcements")
    assert "Codeforces Round 999" in page.text
    assert "Зарегистрироваться" in page.text
    assert "data-countdown" in page.text


async def test_link_must_be_http(session, client):
    await client.post("/login/dev", data={"name": "Кирилл", "teacher": "true"})
    response = await client.post("/teacher/announcements", data={
        "title": "Плохая ссылка", "url": "javascript:alert(1)",
    })
    assert "должна начинаться с http" in response.text
    assert await session.scalar(select(Announcement)) is None


async def test_announcements_page_empty_state(session, client):
    await client.post("/login/dev", data={"name": "Аня", "teacher": "true"})
    response = await client.get("/announcements")
    assert "Объявлений пока нет" in response.text


async def test_student_hides_a_pinned_announcement(session, client):
    """Закреплённый анонс висит у всей группы, а мешает — конкретному человеку."""
    await _login(client, "Кирилл", teacher=True)
    session.add(Announcement(title="Сбор в 17:45", pinned=True,
                             starts_at=utcnow() + timedelta(hours=2)))
    await session.commit()
    await client.post("/logout")

    await _login(client, "Аня")
    assert "Сбор в 17:45" in (await client.get("/")).text

    item = await session.scalar(select(Announcement))
    await client.post(f"/announcements/{item.id}/hide", data={"next": "/"})
    assert "Сбор в 17:45" not in (await client.get("/")).text
    # На своей странице анонс остаётся: убрали с главной, а не удалили.
    assert "Сбор в 17:45" in (await client.get("/announcements")).text


async def test_hidden_announcement_comes_back(session, client):
    await _login(client, "Кирилл", teacher=True)
    session.add(Announcement(title="Сбор в 17:45", pinned=True,
                             starts_at=utcnow() + timedelta(hours=2)))
    await session.commit()
    await client.post("/logout")

    await _login(client, "Аня")
    item = await session.scalar(select(Announcement))
    await client.post(f"/announcements/{item.id}/hide", data={"next": "/"})
    await client.post(f"/announcements/{item.id}/show")
    assert "Сбор в 17:45" in (await client.get("/")).text


async def test_hiding_is_personal(session, client):
    """Аня убрала — у Бори осталось: иначе это снятие закрепления за всех."""
    await _login(client, "Кирилл", teacher=True)
    session.add(Announcement(title="Сбор в 17:45", pinned=True,
                             starts_at=utcnow() + timedelta(hours=2)))
    await session.commit()
    await client.post("/logout")

    await _login(client, "Аня")
    item = await session.scalar(select(Announcement))
    await client.post(f"/announcements/{item.id}/hide", data={"next": "/"})
    await client.post("/logout")

    await _login(client, "Боря")
    assert "Сбор в 17:45" in (await client.get("/")).text
