"""Ядро системы: что засчитывается, а что нет."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

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
)
from app.services.progress import compute_progress

BASE = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
async def world(session):
    student = User(display_name="Аня")
    other = User(display_name="Боря")
    session.add_all([student, other])
    await session.commit()

    group = Group(title="Группа", join_code="ABC123")
    session.add(group)
    await session.commit()
    session.add_all(
        [
            GroupMembership(group_id=group.id, user_id=student.id),
            GroupMembership(group_id=group.id, user_id=other.id),
        ]
    )

    problems = [
        Problem(platform=Platform.leetcode, external_id=str(i), slug=f"p{i}",
                title=f"Задача {i}", url=f"https://leetcode.com/problems/p{i}/",
                difficulty="Medium")
        for i in (1, 2, 3)
    ]
    session.add_all(problems)
    await session.commit()

    problem_set = ProblemSet(title="Список")
    session.add(problem_set)
    await session.commit()
    session.add_all(
        [
            ProblemSetItem(problem_set_id=problem_set.id, problem_id=p.id, position=i)
            for i, p in enumerate(problems)
        ]
    )

    account = PlatformAccount(user_id=student.id, platform=Platform.leetcode,
                              handle="anya", verified_at=BASE)
    session.add(account)
    await session.commit()
    return {"student": student, "other": other, "group": group,
            "problems": problems, "set": problem_set, "account": account}


async def _accept(session, world, problem, when: datetime, external_id: str) -> None:
    session.add(
        Submission(
            user_id=world["student"].id,
            platform_account_id=world["account"].id,
            platform=Platform.leetcode,
            external_id=external_id,
            problem_id=problem.id,
            problem_slug=problem.slug,
            verdict="Accepted",
            is_accepted=True,
            submitted_at=when,
        )
    )
    await session.commit()


async def _assignment(session, world, *, assigned_at, deadline=None, count_prior=False):
    assignment = Assignment(
        title="Задание",
        problem_set_id=world["set"].id,
        group_id=world["group"].id,
        assigned_at=assigned_at,
        deadline=deadline,
        count_prior_solves=count_prior,
    )
    session.add(assignment)
    await session.commit()
    return assignment


async def test_solved_before_assignment_is_not_counted(session, world):
    await _accept(session, world, world["problems"][0], BASE - timedelta(days=10), "s1")
    assignment = await _assignment(session, world, assigned_at=BASE)

    progress = await compute_progress(session, assignment, [world["student"]])
    cell = progress.cell(world["student"].id, world["problems"][0].id)
    assert cell.status == SolveStatus.solved_before
    assert cell.counts is False
    assert progress.solved_count(world["student"].id) == 0


async def test_prior_solves_counted_when_teacher_allows(session, world):
    await _accept(session, world, world["problems"][0], BASE - timedelta(days=10), "s1")
    assignment = await _assignment(session, world, assigned_at=BASE, count_prior=True)

    progress = await compute_progress(session, assignment, [world["student"]])
    assert progress.solved_count(world["student"].id) == 1


async def test_in_time_and_late(session, world):
    deadline = BASE + timedelta(days=7)
    await _accept(session, world, world["problems"][0], BASE + timedelta(days=1), "s1")
    await _accept(session, world, world["problems"][1], deadline + timedelta(hours=1), "s2")
    assignment = await _assignment(session, world, assigned_at=BASE, deadline=deadline)

    progress = await compute_progress(session, assignment, [world["student"]])
    student_id = world["student"].id
    assert progress.cell(student_id, world["problems"][0].id).status == SolveStatus.solved_in_time
    assert progress.cell(student_id, world["problems"][1].id).status == SolveStatus.solved_late
    assert progress.cell(student_id, world["problems"][2].id).status == SolveStatus.not_solved
    # Опоздание видно значком, но в зачёт не идёт: иначе «до дедлайна» ничего
    # не значит. Решение принято к сведению, а счёт — только за срок.
    assert progress.solved_count(student_id) == 1


async def test_resolve_after_prior_solve(session, world):
    """Решил год назад и перерешал после выдачи — засчитываем."""
    problem = world["problems"][0]
    await _accept(session, world, problem, BASE - timedelta(days=365), "old")
    await _accept(session, world, problem, BASE + timedelta(days=2), "new")
    assignment = await _assignment(session, world, assigned_at=BASE)

    progress = await compute_progress(session, assignment, [world["student"]])
    cell = progress.cell(world["student"].id, problem.id)
    assert cell.status == SolveStatus.solved_in_time
    assert cell.solved_at == BASE + timedelta(days=2)
    assert cell.first_ever_at == BASE - timedelta(days=365)


async def test_participants_come_from_group(session, world):
    assignment = await _assignment(session, world, assigned_at=BASE)
    progress = await compute_progress(session, assignment)
    assert {u.display_name for u in progress.participants} == {"Аня", "Боря"}
    assert progress.is_complete(world["other"].id) is False


async def test_complete_when_all_solved(session, world):
    for i, problem in enumerate(world["problems"]):
        await _accept(session, world, problem, BASE + timedelta(days=1), f"s{i}")
    assignment = await _assignment(session, world, assigned_at=BASE)

    progress = await compute_progress(session, assignment, [world["student"]])
    assert progress.is_complete(world["student"].id) is True
    assert progress.problem_solved_count(world["problems"][0].id) == 1


async def test_late_solve_is_visible_but_gives_nothing(session, world):
    """После дедлайна решение видно значком, а счёта не даёт — у всех заданий."""
    deadline = BASE + timedelta(days=7)
    await _accept(session, world, world["problems"][0], deadline + timedelta(hours=1), "s1")
    assignment = await _assignment(session, world, assigned_at=BASE, deadline=deadline)

    progress = await compute_progress(session, assignment, [world["student"]])
    cell = progress.cell(world["student"].id, world["problems"][0].id)
    assert cell.status == SolveStatus.solved_late
    assert cell.counts is False
    assert progress.solved_count(world["student"].id) == 0


async def test_club_wide_assignment_includes_everyone(session, world):
    """Без группы и без студента — задание для всех."""
    from app.models import Assignment

    assignment = Assignment(title="Всем", problem_set_id=world["set"].id, assigned_at=BASE)
    session.add(assignment)
    await session.commit()

    progress = await compute_progress(session, assignment)
    assert {u.display_name for u in progress.participants} == {"Аня", "Боря"}
