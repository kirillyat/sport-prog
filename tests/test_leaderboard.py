from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import (
    Assignment,
    BonusPoint,
    Group,
    GroupMembership,
    Platform,
    PlatformAccount,
    Problem,
    ProblemSet,
    ProblemSetItem,
    Submission,
    User,
    utcnow,
)
from app.services.leaderboard import build_leaderboard

BASE = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
async def world(session):
    anya, borya = User(display_name="Аня"), User(display_name="Боря")
    session.add_all([anya, borya])
    await session.commit()

    group = Group(title="Группа", join_code="ABC123")
    session.add(group)
    await session.commit()
    session.add_all([
        GroupMembership(group_id=group.id, user_id=anya.id),
        GroupMembership(group_id=group.id, user_id=borya.id),
    ])

    problems = [
        Problem(platform=Platform.leetcode, external_id=str(i), slug=f"p{i}", title=f"З{i}",
                url="", difficulty="Medium")
        for i in (1, 2)
    ]
    session.add_all(problems)
    await session.commit()

    problem_set = ProblemSet(title="Список")
    session.add(problem_set)
    await session.commit()
    session.add_all([
        ProblemSetItem(problem_set_id=problem_set.id, problem_id=p.id, position=i)
        for i, p in enumerate(problems)
    ])

    accounts = {}
    for user in (anya, borya):
        acc = PlatformAccount(user_id=user.id, platform=Platform.leetcode,
                              handle=user.display_name, verified_at=BASE)
        session.add(acc)
        await session.commit()
        accounts[user.id] = acc

    return {"anya": anya, "borya": borya, "group": group,
            "problems": problems, "set": problem_set, "accounts": accounts}


async def _accept(session, world, user, problem, when, external_id):
    session.add(Submission(
        user_id=user.id, platform_account_id=world["accounts"][user.id].id,
        platform=Platform.leetcode, external_id=external_id, problem_id=problem.id,
        problem_slug=problem.slug, verdict="Accepted", is_accepted=True, submitted_at=when,
    ))
    await session.commit()


async def test_place_counts_problems_solved_in_time(session, world):
    session.add(Assignment(title="З", problem_set_id=world["set"].id,
                           group_id=world["group"].id, assigned_at=BASE))
    await session.commit()

    anya, borya = world["anya"], world["borya"]
    p1, p2 = world["problems"]
    await _accept(session, world, anya, p1, BASE + timedelta(hours=1), "a1")
    await _accept(session, world, anya, p2, BASE + timedelta(hours=2), "a2")
    await _accept(session, world, borya, p1, BASE + timedelta(hours=3), "b1")

    rows = await build_leaderboard(session)
    by_name = {r.user.display_name: r for r in rows}
    assert (by_name["Аня"].solved, by_name["Аня"].assigned) == (2, 2)
    assert (by_name["Боря"].solved, by_name["Боря"].assigned) == (1, 2)
    assert [r.user.display_name for r in rows] == ["Аня", "Боря"]
    assert [r.place for r in rows] == [1, 2]


async def test_equal_score_is_broken_by_who_finished_earlier(session, world):
    session.add(Assignment(title="З", problem_set_id=world["set"].id,
                           group_id=world["group"].id, assigned_at=BASE))
    await session.commit()

    anya, borya = world["anya"], world["borya"]
    p1, p2 = world["problems"]
    # Оба решили по одной задаче, но Боря закончил раньше.
    await _accept(session, world, borya, p1, BASE + timedelta(hours=1), "b1")
    await _accept(session, world, anya, p2, BASE + timedelta(hours=5), "a1")

    rows = await build_leaderboard(session)
    assert [r.solved for r in rows] == [1, 1]
    assert rows[0].user.display_name == "Боря"


async def test_late_solve_does_not_count_but_is_shown(session, world):
    session.add(Assignment(title="З", problem_set_id=world["set"].id,
                           group_id=world["group"].id, assigned_at=BASE,
                           deadline=BASE + timedelta(hours=2)))
    await session.commit()
    await _accept(session, world, world["anya"], world["problems"][0],
                  BASE + timedelta(hours=6), "a1")

    rows = await build_leaderboard(session)
    by_name = {r.user.display_name: r for r in rows}
    assert by_name["Аня"].solved == 0
    assert by_name["Аня"].late == 1


async def test_prior_solve_does_not_count(session, world):
    session.add(Assignment(title="З", problem_set_id=world["set"].id,
                           group_id=world["group"].id, assigned_at=BASE))
    await session.commit()
    await _accept(session, world, world["anya"], world["problems"][0],
                  BASE - timedelta(days=30), "old")

    rows = await build_leaderboard(session)
    assert all(r.solved == 0 for r in rows)


async def test_hard_deadline_leaves_nothing_for_latecomers(session, world):
    """Марафон: после срока решение не засчитывается вовсе, даже как опоздание."""
    session.add(Assignment(
        title="Марафон", problem_set_id=world["set"].id, group_id=world["group"].id,
        assigned_at=BASE, deadline=BASE + timedelta(hours=8), hard_deadline=True,
    ))
    await session.commit()

    anya, borya = world["anya"], world["borya"]
    p1, p2 = world["problems"]
    await _accept(session, world, anya, p1, BASE + timedelta(hours=1), "a1")
    await _accept(session, world, anya, p2, BASE + timedelta(hours=2), "a2")
    await _accept(session, world, borya, p1, BASE + timedelta(hours=9), "b1")

    rows = await build_leaderboard(session)
    by_name = {r.user.display_name: r for r in rows}
    assert by_name["Аня"].solved == 2
    assert (by_name["Боря"].solved, by_name["Боря"].late) == (0, 0)


async def test_club_wide_assignment_counts_for_everyone(session, world):
    """Задание без группы — для всех."""
    session.add(Assignment(title="Всем", problem_set_id=world["set"].id, assigned_at=BASE))
    await session.commit()
    await _accept(session, world, world["borya"], world["problems"][0],
                  BASE + timedelta(hours=1), "b1")

    rows = await build_leaderboard(session)
    by_name = {r.user.display_name: r for r in rows}
    assert by_name["Боря"].solved == 1
    assert by_name["Аня"].assigned == 2


async def test_manual_bonus_is_shown_but_does_not_move_anyone(session, world):
    """Бонус жюри — признание, а не валюта: место он не меняет."""
    session.add(Assignment(title="З", problem_set_id=world["set"].id,
                           group_id=world["group"].id, assigned_at=BASE))
    session.add(BonusPoint(user_id=world["borya"].id, points=7.5,
                           reason="разбор на семинаре", granted_at=BASE))
    await session.commit()
    await _accept(session, world, world["anya"], world["problems"][0],
                  BASE + timedelta(hours=1), "a1")

    rows = await build_leaderboard(session)
    by_name = {r.user.display_name: r for r in rows}
    assert by_name["Боря"].bonus == 7.5
    assert rows[0].user.display_name == "Аня"


async def test_movement_shows_the_week(session, world):
    """Место неделю назад считается по тем же решениям, отсечённым по времени."""
    now = utcnow()
    session.add(Assignment(title="З", problem_set_id=world["set"].id,
                           group_id=world["group"].id, assigned_at=now - timedelta(days=30)))
    await session.commit()

    anya, borya = world["anya"], world["borya"]
    p1, p2 = world["problems"]
    # Боря вёл неделю назад, Аня обогнала его за последние дни.
    await _accept(session, world, borya, p1, now - timedelta(days=10), "b1")
    await _accept(session, world, anya, p1, now - timedelta(days=2), "a1")
    await _accept(session, world, anya, p2, now - timedelta(days=1), "a2")

    rows = await build_leaderboard(session)
    by_name = {r.user.display_name: r for r in rows}
    assert by_name["Аня"].place == 1
    assert by_name["Аня"].movement == 1   # была второй
    assert by_name["Боря"].movement == -1


async def test_no_movement_without_history(session, world):
    """Пока недельной истории нет, стрелки не показываем — иначе они врут."""
    session.add(Assignment(title="З", problem_set_id=world["set"].id,
                           group_id=world["group"].id, assigned_at=utcnow()))
    await session.commit()
    await _accept(session, world, world["anya"], world["problems"][0], utcnow(), "a1")

    rows = await build_leaderboard(session)
    assert all(r.movement is None for r in rows)


async def test_group_filter_narrows_scope(session, world):
    outsider = User(display_name="Вова")
    session.add(outsider)
    await session.commit()

    everyone = await build_leaderboard(session)
    assert len(everyone) == 3

    in_group = await build_leaderboard(session, group_id=world["group"].id)
    assert {r.user.display_name for r in in_group} == {"Аня", "Боря"}


async def test_first_solver_is_the_earliest_and_needs_a_rival(session, world):
    from app.services.progress import compute_progress

    assignment = Assignment(title="З", problem_set_id=world["set"].id,
                            group_id=world["group"].id, assigned_at=BASE)
    session.add(assignment)
    await session.commit()

    anya, borya = world["anya"], world["borya"]
    p1 = world["problems"][0]
    await _accept(session, world, borya, p1, BASE + timedelta(hours=1), "b1")
    await _accept(session, world, anya, p1, BASE + timedelta(hours=2), "a1")

    both = await compute_progress(session, assignment, [anya, borya])
    assert both.first_solver(p1.id).display_name == "Боря"
    assert both.first_solver(world["problems"][1].id) is None  # никто не решил

    # В одиночку соревноваться не с кем — отметки нет.
    alone = await compute_progress(session, assignment, [anya])
    assert alone.first_solver(p1.id) is None


async def test_teachers_stay_out_of_the_ranking(session, world):
    from app.models import Role

    anya = world["anya"]
    anya.role = Role.teacher
    await session.commit()

    rows = await build_leaderboard(session)
    assert [r.user.display_name for r in rows] == ["Боря"]

    in_group = await build_leaderboard(session, group_id=world["group"].id)
    assert [r.user.display_name for r in in_group] == ["Боря"]


# --- страница табло: только по группам ---------------------------------------


async def _login(client, name, teacher=False):
    data = {"name": name}
    if teacher:
        data["teacher"] = "true"
    await client.post("/login/dev", data=data)


async def test_board_page_has_no_all_option(session, client):
    """Общего зачёта нет: у разных групп разные задания, сравнивать нечего."""
    await _login(client, "Кирилл", teacher=True)
    await client.post("/teacher/groups", data={"title": "Осень"})

    page = (await client.get("/leaderboard")).text
    assert '<option value="">Все</option>' not in page
    assert "Осень" in page


async def test_board_defaults_to_the_first_own_group(session, client):
    await _login(client, "Кирилл", teacher=True)
    await client.post("/teacher/groups", data={"title": "Алгоритмы"})
    group = await session.scalar(select(Group).where(Group.title == "Алгоритмы"))
    await client.post("/logout")

    await _login(client, "Аня")
    await client.post("/groups/join", data={"join_code": group.join_code})

    page = (await client.get("/leaderboard")).text
    assert f'value="{group.id}" selected' in page


async def test_student_without_groups_sees_an_explanation(session, client):
    await _login(client, "Кирилл", teacher=True)
    await client.post("/logout")
    await _login(client, "Аня")

    page = (await client.get("/leaderboard")).text
    assert "Вступи в группу" in page
