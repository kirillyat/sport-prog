"""«Когда решали»: посылки участников задания по времени, от выдачи до дедлайна."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
    User,
)
from app.services import timeline
from app.templating import LOCAL_TZ

START = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
DEADLINE = START + timedelta(days=7)
NOW = DEADLINE + timedelta(days=1)


async def _setup(session):
    anya, boris = User(display_name="Аня"), User(display_name="Борис")
    teacher = User(display_name="Кирилл", role=Role.teacher)
    problem = Problem(platform=Platform.leetcode, external_id="1", slug="two-sum",
                      title="Two Sum", url="https://leetcode.com/problems/two-sum/")
    session.add_all([anya, boris, teacher, problem])
    await session.commit()
    group = Group(title="ПМИ-1", join_code="PMI1")
    problem_set = ProblemSet(title="Список")
    session.add_all([group, problem_set])
    await session.commit()
    session.add_all([
        GroupMembership(group_id=group.id, user_id=anya.id),
        GroupMembership(group_id=group.id, user_id=boris.id),
        GroupMembership(group_id=group.id, user_id=teacher.id),
        ProblemSetItem(problem_set_id=problem_set.id, problem_id=problem.id),
    ])
    assignment = Assignment(title="ДЗ", problem_set_id=problem_set.id, group_id=group.id,
                            assigned_at=START, deadline=DEADLINE)
    session.add(assignment)
    await session.commit()
    return assignment, [anya, boris, teacher], [problem]


_ids = iter(range(1, 10_000))


def _sub(user, problem, moment, accepted):
    return Submission(user_id=user.id, platform=Platform.leetcode, external_id=f"s{next(_ids)}",
                      problem_id=problem.id, verdict="Accepted" if accepted else "Wrong Answer",
                      is_accepted=accepted, submitted_at=moment)


def test_a_week_is_shown_in_six_hour_columns():
    """Не больше полусотни столбиков: мельче — гребёнка на экране ноутбука."""
    assert timeline.pick_step(timedelta(days=7)) == timedelta(hours=6)
    assert timeline.pick_step(timedelta(hours=10)) == timedelta(hours=1)
    assert timeline.pick_step(timedelta(days=60)) == timedelta(days=2)


async def test_first_solve_counts_once_and_the_rest_are_attempts(session):
    assignment, (anya, boris, teacher), (problem,) = await _setup(session)
    last_evening = DEADLINE - timedelta(hours=3)
    session.add_all([
        _sub(anya, problem, START + timedelta(days=1), False),
        _sub(anya, problem, START + timedelta(days=1, hours=1), True),   # решение
        _sub(anya, problem, START + timedelta(days=2), True),            # перерешала
        _sub(boris, problem, last_evening, True),                        # впритык
        _sub(teacher, problem, START + timedelta(hours=1), True),        # не участник
        _sub(boris, problem, START - timedelta(days=30), True),          # до выдачи
    ])
    await session.commit()

    built = await timeline.build(session, assignment, [anya, boris, teacher], [problem],
                                 LOCAL_TZ, now=NOW)
    assert built is not None
    when, in_time = built
    assert (when.solves, when.submissions) == (2, 4)
    assert len(in_time) == 2
    # Одно из двух решений пришло в последние сутки — половина.
    assert when.last_day_share(in_time) == 50


async def test_a_solve_after_the_deadline_is_not_in_the_last_day_share(session):
    assignment, (anya, boris, _), (problem,) = await _setup(session)
    session.add_all([
        _sub(anya, problem, DEADLINE - timedelta(days=3), True),
        _sub(boris, problem, DEADLINE + timedelta(hours=2), True),       # опоздал
    ])
    await session.commit()
    when, in_time = await timeline.build(session, assignment, [anya, boris], [problem],
                                         LOCAL_TZ, now=NOW)
    assert when.solves == 2 and len(in_time) == 1
    assert when.last_day_share(in_time) == 0
    # Окно тянется до последней посылки после дедлайна, а не обрывается на нём.
    assert when.bins[-1].end > DEADLINE + timedelta(hours=2)


async def test_columns_start_on_local_hours_not_on_the_issue_minute(session):
    assignment, (anya, _, _), (problem,) = await _setup(session)
    assignment.assigned_at = START + timedelta(minutes=43)
    session.add(_sub(anya, problem, START + timedelta(hours=5), True))
    await session.commit()
    when, _ = await timeline.build(session, assignment, [anya], [problem], LOCAL_TZ, now=NOW)
    first = when.bins[0].start.astimezone(LOCAL_TZ)
    assert (first.minute, first.hour % 6) == (0, 0)


async def test_nothing_to_draw_without_submissions(session):
    assignment, users, problems = await _setup(session)
    assert await timeline.build(session, assignment, users, problems, LOCAL_TZ, now=NOW) is None


async def test_teacher_sees_when_the_group_solved(session, client):
    assignment, (anya, _, _), (problem,) = await _setup(session)
    session.add(_sub(anya, problem, DEADLINE - timedelta(hours=2), True))
    await session.commit()
    assignment_id = assignment.id
    await client.post("/login/dev", data={"name": "Кирилл"})   # тот же, что в группе

    page = (await client.get(f"/teacher/assignments/{assignment_id}")).text
    assert "Когда решали" in page and 'class="timeline"' in page
    assert "100%" in page and "последние сутки перед дедлайном" in page
    assert "<title>" in page                     # подсказка у столбика
