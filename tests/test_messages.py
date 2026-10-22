"""Сообщения преподавателя к заданию: кто их видит, как они выглядят, куда уходят."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app import notify
from app.models import (
    Assignment,
    AssignmentMessage,
    Group,
    GroupMembership,
    ProblemSet,
    Role,
    User,
    utcnow,
)
from app.services import api_tokens, messages
from app.services.linkify import render

ROOM = "https://leetcode.com/playground/AbC123/shared"


async def _setup(session):
    teacher = User(display_name="Кирилл", role=Role.teacher)
    anya, boris = User(display_name="Аня"), User(display_name="Борис")
    session.add_all([teacher, anya, boris])
    await session.commit()
    mine, theirs = Group(title="ПМИ-1", join_code="PMI1"), Group(title="ПМИ-2", join_code="PMI2")
    problem_set = ProblemSet(title="Семинар")
    session.add_all([mine, theirs, problem_set])
    await session.commit()
    session.add_all([
        GroupMembership(group_id=mine.id, user_id=anya.id),
        GroupMembership(group_id=theirs.id, user_id=boris.id),
    ])
    assignment = Assignment(title="Семинар 5", problem_set_id=problem_set.id, group_id=mine.id,
                            assigned_at=utcnow() - timedelta(hours=1))
    session.add(assignment)
    await session.commit()
    # Страница в тестах читает ту же сессию, что и тест, — группа задания
    # должна быть уже загружена, как после обычного запроса к базе.
    await session.refresh(assignment, ["group"])
    return assignment, teacher, anya, boris


async def _as(client, name):
    await client.post("/logout")
    await client.post("/login/dev", data={"name": name})


def test_links_are_clickable_and_everything_else_stays_text():
    html = str(render(f"Комната ({ROOM}). <script>alert(1)</script> javascript:alert(1)"))
    assert f'<a href="{ROOM}" target="_blank" rel="noopener noreferrer">' in html
    assert "<script>" not in html and "&lt;script&gt;" in html
    # Скобка и точка после адреса — конец фразы, а не часть ссылки.
    assert f"{ROOM}</a>)." in html
    assert 'href="javascript' not in html


async def test_the_group_sees_the_room_link_and_another_group_does_not(session, client):
    assignment, _, _, _ = await _setup(session)
    await _as(client, "Кирилл")
    await client.post(f"/teacher/assignments/{assignment.id}/messages",
                      data={"body": f"Заходите в комнату: {ROOM}"})

    await _as(client, "Аня")
    page = (await client.get(f"/assignments/{assignment.id}")).text
    assert f'href="{ROOM}"' in page and "Открыть ссылку" in page
    home = (await client.get("/")).text
    assert "Только что от преподавателя" in home and ROOM in home

    await _as(client, "Борис")
    assert ROOM not in (await client.get("/")).text
    hidden = await client.get(f"/assignments/{assignment.id}/messages")
    assert hidden.status_code == 404 and hidden.json() == {"items": []}


async def test_the_page_picks_up_only_newer_messages(session, client):
    assignment, teacher, _, _ = await _setup(session)
    first = await messages.post(session, assignment, teacher, "Начинаем")
    second = await messages.post(session, assignment, teacher, f"Комната: {ROOM}")
    await _as(client, "Аня")
    found = (await client.get(f"/assignments/{assignment.id}/messages?after={first.id}")).json()
    assert [item["id"] for item in found["items"]] == [second.id]
    assert ROOM in found["items"][0]["html"]


async def test_the_teacher_stays_where_the_message_was_written(session, client):
    """Из матрицы — обратно в матрицу, со страницы студента — на неё."""
    assignment, _, _, _ = await _setup(session)
    await _as(client, "Кирилл")
    matrix = f"http://test/teacher/assignments/{assignment.id}"
    student_page = f"http://test/assignments/{assignment.id}"
    for page in (matrix, student_page):
        response = await client.post(f"/teacher/assignments/{assignment.id}/messages",
                                     data={"body": "привет"}, headers={"Referer": page})
        assert str(response.url).split("#")[0] == page


async def test_a_student_cannot_post(session, client):
    assignment, _, _, _ = await _setup(session)
    await _as(client, "Аня")
    response = await client.post(f"/teacher/assignments/{assignment.id}/messages",
                                 data={"body": "я тоже хочу"})
    assert response.status_code == 403
    assert await session.scalar(select(AssignmentMessage)) is None


async def test_the_teacher_can_take_a_message_back(session, client):
    assignment, teacher, _, _ = await _setup(session)
    wrong = await messages.post(session, assignment, teacher, "не та ссылка")
    await _as(client, "Кирилл")
    await client.post(f"/teacher/assignments/{assignment.id}/messages/{wrong.id}/delete")
    assert await session.scalar(select(AssignmentMessage)) is None


async def test_a_message_leaves_the_home_page_after_the_class(session):
    assignment, teacher, anya, _ = await _setup(session)
    await messages.post(session, assignment, teacher, f"Комната: {ROOM}")
    assert len(await messages.fresh_for(session, anya)) == 1
    later = utcnow() + messages.FRESH + timedelta(minutes=1)
    assert await messages.fresh_for(session, anya, now=later) == []


async def test_telegram_only_when_asked(session, monkeypatch):
    """Сообщения на паре затем и пишутся на портале, чтобы не дёргать мессенджер."""
    assignment, teacher, _, _ = await _setup(session)
    sent = []

    async def fake(item, target, session=None):
        sent.append(item.body)
        return 1

    monkeypatch.setattr(notify, "notify_assignment_message", fake)
    await messages.post(session, assignment, teacher, "только на портале")
    await messages.post(session, assignment, teacher, "и в мессенджер", telegram=True)
    assert sent == ["и в мессенджер"]


async def test_empty_or_endless_messages_are_refused(session):
    assignment, teacher, _, _ = await _setup(session)
    assert isinstance(await messages.post(session, assignment, teacher, "   \n "), str)
    too_long = "x" * (messages.MAX_CHARS + 1)
    assert isinstance(await messages.post(session, assignment, teacher, too_long), str)


async def test_the_ai_posts_only_with_a_writing_key(session, client):
    assignment, teacher, anya, _ = await _setup(session)
    assignment_id, teacher_id, anya_id = assignment.id, teacher.id, anya.id

    async def call(key, name, **arguments):
        response = await client.post("/mcp", headers={"Authorization": f"Bearer {key}"}, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        return response.json()["result"]

    _, reading = await api_tokens.issue(session, teacher, "чтение", can_write=False)
    refused = await call(reading, "post_assignment_message",
                         assignment_id=assignment_id, text=ROOM)
    assert refused["isError"]

    # Отказ откатывает сессию, общую с тестом, — учётку берём заново.
    teacher = await session.get(User, teacher_id)
    anya = await session.get(User, anya_id)
    _, writing = await api_tokens.issue(session, teacher, "запись", can_write=True)
    posted = await call(writing, "post_assignment_message", assignment_id=assignment_id, text=ROOM)
    assert posted["structuredContent"]["message"]["text"] == ROOM

    _, student_key = await api_tokens.issue(session, anya, "студент", can_write=False)
    seen = await call(student_key, "get_assignment", assignment_id=assignment_id)
    assert seen["structuredContent"]["messages"][0]["text"] == ROOM
