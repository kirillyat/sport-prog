"""Выдача одного списка нескольким группам и перенос дедлайна."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app import cli
from app.models import (
    Assignment,
    Group,
    GroupMembership,
    Platform,
    PlatformAccount,
    Problem,
    ProblemSet,
    ProblemSetItem,
    SolveStatus,
    Submission,
    User,
    utcnow,
)
from app.services.deadlines import extend_deadline
from app.services.progress import compute_progress
from app.templating import to_local_input

BASE = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


async def _login(client: httpx.AsyncClient, name: str, teacher: bool = False):
    data = {"name": name}
    if teacher:
        data["teacher"] = "true"
    return await client.post("/login/dev", data=data)


@pytest.fixture
async def world(session):
    student = User(display_name="Аня")
    session.add(student)
    group = Group(title="Группа 101", join_code="ABC123")
    session.add(group)
    problem = Problem(platform=Platform.leetcode, external_id="1", slug="two-sum",
                      title="Two Sum", url="https://leetcode.com/problems/two-sum/")
    session.add(problem)
    problem_set = ProblemSet(title="Семинар 3")
    session.add(problem_set)
    await session.commit()
    session.add_all([
        GroupMembership(group_id=group.id, user_id=student.id),
        ProblemSetItem(problem_set_id=problem_set.id, problem_id=problem.id),
    ])
    account = PlatformAccount(user_id=student.id, platform=Platform.leetcode,
                              handle="anya", verified_at=BASE)
    session.add(account)
    await session.commit()
    return {"student": student, "group": group, "problem": problem,
            "set": problem_set, "account": account}


async def _late_solve(session, world, deadline: datetime) -> Assignment:
    """Задание, которое студент решил через сутки после срока."""
    session.add(Submission(
        user_id=world["student"].id, platform_account_id=world["account"].id,
        platform=Platform.leetcode, external_id="s1", problem_id=world["problem"].id,
        problem_slug="two-sum", verdict="Accepted", is_accepted=True,
        submitted_at=deadline + timedelta(days=1),
    ))
    assignment = Assignment(title="Семинар 3", problem_set_id=world["set"].id,
                            group_id=world["group"].id, assigned_at=BASE, deadline=deadline)
    session.add(assignment)
    await session.commit()
    return assignment


# ---------------------------------------------------------------- несколько групп


async def test_assignment_goes_to_every_checked_group(session, client):
    await _login(client, "Кирилл", teacher=True)
    problem_set = ProblemSet(title="Семинар 3")
    first, second, untouched = (
        Group(title=f"Группа {n}", join_code=f"CODE0{n}") for n in (1, 2, 3)
    )
    session.add_all([problem_set, first, second, untouched])
    await session.commit()

    response = await client.post("/teacher/assignments", data={
        "title": "Семинар 3", "problem_set_id": problem_set.id,
        "group_id": [str(first.id), str(second.id), str(first.id)],  # повтор не плодит копию
        "deadline": "2026-12-01T18:00",
    })
    assert "Задание выдано группам: 2" in response.text

    items = (await session.execute(select(Assignment))).scalars().all()
    # По заданию на группу: у каждой своё табло и свой срок, который можно
    # продлить, не задев соседей.
    assert sorted(item.group_id for item in items) == sorted([first.id, second.id])
    assert len({item.deadline for item in items}) == 1


async def test_unknown_group_cancels_the_whole_assignment(session, client):
    await _login(client, "Кирилл", teacher=True)
    problem_set = ProblemSet(title="Семинар 3")
    group = Group(title="Группа 1", join_code="CODE01")
    session.add_all([problem_set, group])
    await session.commit()

    response = await client.post("/teacher/assignments", data={
        "title": "Семинар 3", "problem_set_id": problem_set.id,
        "group_id": [str(group.id), "999"],
    })
    assert "Группа не найдена" in response.text
    assert await session.scalar(select(Assignment)) is None


async def test_form_offers_groups_as_checkboxes(session, client):
    await _login(client, "Кирилл", teacher=True)
    session.add_all([ProblemSet(title="Семинар 3"), Group(title="Группа 1", join_code="CODE01")])
    await session.commit()

    page = (await client.get("/teacher/assignments")).text
    assert 'type="checkbox" name="group_id"' in page


# ---------------------------------------------------------------- перенос срока


async def test_extended_deadline_credits_a_late_solve(session, world):
    deadline = BASE + timedelta(days=3)
    assignment = await _late_solve(session, world, deadline)
    student_id, problem_id = world["student"].id, world["problem"].id

    progress = await compute_progress(session, assignment)
    assert progress.cell(student_id, problem_id).status == SolveStatus.solved_late

    assert extend_deadline(assignment, deadline + timedelta(days=2)) is None
    progress = await compute_progress(session, assignment)
    assert progress.cell(student_id, problem_id).status == SolveStatus.solved_in_time
    assert progress.solved_count(student_id) == 1


async def test_deadline_cannot_move_back(session, world):
    """Ранний срок снял бы зачёт с тех, кто честно уложился в прежний."""
    deadline = BASE + timedelta(days=3)
    assignment = await _late_solve(session, world, deadline)

    assert extend_deadline(assignment, deadline - timedelta(hours=1)) is not None
    assert extend_deadline(assignment, deadline) is not None
    assert assignment.deadline == deadline


async def test_assignment_without_deadline_does_not_get_one(session, world):
    assignment = Assignment(title="Без срока", problem_set_id=world["set"].id, assigned_at=BASE)
    session.add(assignment)
    await session.commit()

    assert extend_deadline(assignment, BASE + timedelta(days=1)) is not None
    assert assignment.deadline is None


async def test_extended_deadline_reminds_again(session, world):
    assignment = await _late_solve(session, world, BASE + timedelta(days=3))
    assignment.reminded_at = BASE + timedelta(days=2)

    extend_deadline(assignment, utcnow() + timedelta(days=2))
    # Прежнее напоминание было про старую дату.
    assert assignment.reminded_at is None


async def test_teacher_shifts_deadline_from_assignment_page(session, client, world):
    await _login(client, "Кирилл", teacher=True)
    deadline = BASE + timedelta(days=3)
    assignment = await _late_solve(session, world, deadline)

    page = (await client.get(f"/teacher/assignments/{assignment.id}")).text
    assert f'action="/teacher/assignments/{assignment.id}/deadline"' in page

    response = await client.post(f"/teacher/assignments/{assignment.id}/deadline", data={
        "deadline": to_local_input(deadline - timedelta(days=1)),
    })
    assert "можно только отодвинуть" in response.text

    new_deadline = deadline + timedelta(days=5)
    response = await client.post(f"/teacher/assignments/{assignment.id}/deadline", data={
        "deadline": to_local_input(new_deadline),
    })
    assert "Дедлайн сдвинут" in response.text
    await session.refresh(assignment)
    assert assignment.deadline == new_deadline


async def test_teacher_removes_deadline(session, client, world):
    await _login(client, "Кирилл", teacher=True)
    assignment = await _late_solve(session, world, BASE + timedelta(days=3))

    response = await client.post(f"/teacher/assignments/{assignment.id}/deadline", data={
        "deadline": "", "remove": "true",
    })
    assert "Дедлайн снят" in response.text
    await session.refresh(assignment)
    assert assignment.deadline is None


# ---------------------------------------------------------------- массовый перенос


async def test_bulk_extension_dry_run_changes_nothing(session, db, world, monkeypatch, capsys):
    monkeypatch.setattr(cli, "SessionLocal", db)
    deadline = BASE + timedelta(days=3)
    assignment = await _late_solve(session, world, deadline)

    code = await cli.extend_deadlines("2026-10-03T23:59", [assignment.id], apply=False)
    assert code == 0
    assert "зачётов 0 → 1 (+1)" in capsys.readouterr().out
    await session.refresh(assignment)
    assert assignment.deadline == deadline


async def test_bulk_extension_is_all_or_nothing(session, db, world, monkeypatch, capsys):
    """Половина продлённых заданий хуже ни одного: не вспомнить, какие сдвинуты."""
    monkeypatch.setattr(cli, "SessionLocal", db)
    deadline = BASE + timedelta(days=3)
    assignment = await _late_solve(session, world, deadline)

    code = await cli.extend_deadlines("2026-10-03T23:59", [assignment.id, 999], apply=True)
    assert code == 1
    await session.refresh(assignment)
    assert assignment.deadline == deadline


async def test_bulk_extension_backs_up_then_writes(
    session, db, world, monkeypatch, tmp_path, capsys
):
    from app.config import settings

    monkeypatch.setattr(cli, "SessionLocal", db)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    # Копия снимается с DATA_DIR/sport.db, а тестовая база названа иначе.
    (tmp_path / "sport.db").symlink_to(tmp_path / "test.db")
    assignment = await _late_solve(session, world, BASE + timedelta(days=3))

    code = await cli.extend_deadlines("2026-10-03T23:59", [assignment.id], apply=True)
    assert code == 0
    assert list(tmp_path.glob("backup-before-extend-*.db"))
    await session.refresh(assignment)
    assert to_local_input(assignment.deadline) == "2026-10-03T23:59"
