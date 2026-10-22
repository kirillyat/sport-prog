"""Начало задания — момент, когда его видят студенты. И задачи можно убрать
из уже выданного задания."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app import notify
from app.models import (
    Assignment,
    Group,
    GroupMembership,
    Platform,
    Problem,
    ProblemSet,
    ProblemSetItem,
    Role,
    Submission,
    Task,
    User,
    utcnow,
)
from app.services import api_tokens, assignments
from app.services.leaderboard import build_leaderboard

TOMORROW = timedelta(days=1)


async def _world(session, *, starts_in=TOMORROW):
    teacher = User(display_name="Кирилл", role=Role.teacher)
    anya = User(display_name="Аня")
    two_sum = Problem(platform=Platform.leetcode, external_id="1", slug="two-sum",
                      title="Two Sum", url="https://leetcode.com/problems/two-sum/")
    exam_task = Problem(platform=Platform.local, external_id="exam-a", slug="exam-a",
                        title="Секретная задача", url="/tasks/exam-a")
    session.add_all([teacher, anya, two_sum, exam_task])
    await session.commit()
    session.add(Task(problem_id=exam_task.id, statement="Условие контрольной."))
    group = Group(title="ПМИ-1", join_code="PMI1")
    problem_set = ProblemSet(title="Контрольная")
    session.add_all([group, problem_set])
    await session.commit()
    session.add_all([
        GroupMembership(group_id=group.id, user_id=anya.id),
        ProblemSetItem(problem_set_id=problem_set.id, problem_id=two_sum.id, position=0),
        ProblemSetItem(problem_set_id=problem_set.id, problem_id=exam_task.id, position=1),
    ])
    exam = Assignment(title="Контрольная 1", problem_set_id=problem_set.id, group_id=group.id,
                      assigned_at=utcnow() + starts_in, deadline=utcnow() + starts_in + TOMORROW)
    session.add(exam)
    await session.commit()
    await session.refresh(exam, ["group"])
    return exam, teacher, anya, group, problem_set, two_sum


async def _as(client, name):
    await client.post("/logout")
    await client.post("/login/dev", data={"name": name})


async def _mcp(client, key, name, **arguments):
    response = await client.post("/mcp", headers={"Authorization": f"Bearer {key}"}, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    return response.json()["result"]


async def test_a_future_exam_is_invisible_to_students_until_it_starts(session, client):
    exam, _, anya, _, _, _ = await _world(session)
    exam_id = exam.id
    _, key = await api_tokens.issue(session, anya, "студент", can_write=False)

    await _as(client, "Аня")
    assert "Контрольная 1" not in (await client.get("/")).text
    page = await client.get(f"/assignments/{exam_id}")
    assert "Контрольная 1" not in page.text.split("<main", 1)[1]
    listed = await _mcp(client, key, "list_assignments")
    assert listed["structuredContent"]["assignments"] == []
    assert (await _mcp(client, key, "get_assignment", assignment_id=exam_id))["isError"]

    # Началось — видно везде.
    exam = await session.get(Assignment, exam_id)
    exam.assigned_at = utcnow() - timedelta(minutes=1)
    await session.commit()
    assert "Контрольная 1" in (await client.get("/")).text
    assert "Two Sum" in (await client.get(f"/assignments/{exam_id}")).text


async def test_a_task_of_a_future_exam_does_not_open_by_link(session, client):
    """Слаг из архива угадать нетрудно — условие не должно открываться."""
    await _world(session)
    await _as(client, "Аня")
    assert "Условие контрольной" not in (await client.get("/tasks/exam-a")).text
    await _as(client, "Кирилл")
    assert "Условие контрольной" in (await client.get("/tasks/exam-a")).text


async def test_a_task_in_no_assignment_stays_open_for_practice(session, client):
    session.add_all([User(display_name="Кирилл", role=Role.teacher), User(display_name="Аня")])
    practice = Problem(platform=Platform.local, external_id="warmup", slug="warmup",
                       title="Разминка", url="/tasks/warmup")
    session.add(practice)
    await session.commit()
    session.add(Task(problem_id=practice.id, statement="Просто потренироваться."))
    await session.commit()
    await _as(client, "Аня")
    assert "Просто потренироваться" in (await client.get("/tasks/warmup")).text


async def test_a_future_exam_is_not_counted_as_assigned_on_the_leaderboard(session):
    _, _, anya, group, _, _ = await _world(session)
    rows = await build_leaderboard(session, group_id=group.id)
    assert next(r for r in rows if r.user.id == anya.id).assigned == 0


async def test_telegram_waits_for_the_start(session, monkeypatch):
    """Сообщение при выдаче выдало бы контрольную заранее."""
    teacher = User(display_name="Кирилл", role=Role.teacher)
    group, problem_set = Group(title="ПМИ-1", join_code="PMI1"), ProblemSet(title="Список")
    session.add_all([teacher, group, problem_set])
    await session.commit()
    sent = []

    async def fake(item, problems, session=None):
        sent.append(item.title)
        return 1

    monkeypatch.setattr(notify, "notify_assignment", fake)
    created, delivered = await assignments.issue(
        session, teacher, title="Контрольная", problem_set_id=problem_set.id,
        group_ids=[group.id], starts_at=utcnow() + TOMORROW,
    )
    assert (delivered, sent, created[0].announced) == (0, [], False)
    assert await notify.announce_started(session) == 0

    created[0].assigned_at = utcnow() - timedelta(seconds=1)
    await session.commit()
    assert await notify.announce_started(session) == 1
    assert sent == ["Контрольная"] and created[0].announced
    assert await notify.announce_started(session) == 0           # дважды не объявляем


async def test_teacher_removes_a_problem_someone_already_solved(session, client):
    exam, _, anya, _, problem_set, two_sum = await _world(session, starts_in=-timedelta(hours=1))
    exam_id, set_id, two_sum_id = exam.id, problem_set.id, two_sum.id
    session.add(Submission(user_id=anya.id, platform=Platform.leetcode, external_id="s1",
                           problem_id=two_sum.id, verdict="Accepted", is_accepted=True,
                           submitted_at=utcnow()))
    await session.commit()

    await _as(client, "Кирилл")
    page = await client.post(f"/teacher/assignments/{exam_id}/problems/{two_sum_id}/remove")
    assert "Задача убрана" in page.text
    left = (await session.execute(
        select(ProblemSetItem.problem_id).where(ProblemSetItem.problem_set_id == set_id)
    )).scalars().all()
    assert two_sum_id not in left
    # Посылка студента осталась — убрана задача из задания, а не история.
    assert await session.scalar(select(Submission).where(Submission.external_id == "s1"))


async def test_the_ai_removes_problems_only_with_a_writing_key(session, client):
    _, teacher, _, _, problem_set, _ = await _world(session, starts_in=-timedelta(hours=1))
    set_id, teacher_id = problem_set.id, teacher.id
    _, reading = await api_tokens.issue(session, teacher, "чтение", can_write=False)
    refused = await _mcp(client, reading, "remove_problems_from_set",
                         problem_set_id=set_id, problems=["two-sum"])
    assert refused["isError"]

    teacher = await session.get(User, teacher_id)
    _, writing = await api_tokens.issue(session, teacher, "запись", can_write=True)
    done = await _mcp(client, writing, "remove_problems_from_set",
                      problem_set_id=set_id, problems=["two-sum"])
    assert done["structuredContent"]["removed"] == 1
    assert done["structuredContent"]["assignments"][0]["title"] == "Контрольная 1"
