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
    Submission,
    User,
)
from app.services.feed import build_feed, visible_user_ids

BASE = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
async def world(session):
    anya = User(display_name="Аня")
    borya = User(display_name="Боря")
    vova = User(display_name="Вова")  # в другой группе
    teacher = User(display_name="Кирилл", role="teacher")
    session.add_all([anya, borya, vova, teacher])
    await session.commit()

    group = Group(title="Группа А", join_code="AAA111")
    other = Group(title="Группа Б", join_code="BBB222")
    session.add_all([group, other])
    await session.commit()
    session.add_all([
        GroupMembership(group_id=group.id, user_id=anya.id),
        GroupMembership(group_id=group.id, user_id=borya.id),
        GroupMembership(group_id=other.id, user_id=vova.id),
    ])

    problems = [
        Problem(platform=Platform.leetcode, external_id="1", slug="two-sum", title="Two Sum",
                url="https://leetcode.com/problems/two-sum/", difficulty="Easy"),
        Problem(platform=Platform.codeforces, external_id="4A", slug="4a", title="Watermelon",
                url="https://codeforces.com/problemset/problem/4/A", rating=800),
    ]
    session.add_all(problems)
    await session.commit()

    accounts = {}
    for user, platform in ((anya, Platform.leetcode), (borya, Platform.leetcode),
                           (vova, Platform.leetcode)):
        acc = PlatformAccount(user_id=user.id, platform=platform,
                              handle=user.display_name, verified_at=BASE)
        session.add(acc)
        await session.commit()
        accounts[user.id] = acc

    return {"anya": anya, "borya": borya, "vova": vova, "teacher": teacher,
            "group": group, "other": other, "problems": problems, "accounts": accounts}


async def _accept(session, world, user, problem, when, external_id):
    session.add(Submission(
        user_id=user.id, platform_account_id=world["accounts"][user.id].id,
        platform=problem.platform, external_id=external_id, problem_id=problem.id,
        problem_slug=problem.slug, problem_title=problem.title,
        verdict="Accepted", is_accepted=True, submitted_at=when,
    ))
    await session.commit()


async def test_student_sees_only_groupmates(session, world):
    anya, borya, vova = world["anya"], world["borya"], world["vova"]
    p = world["problems"][0]
    await _accept(session, world, anya, p, BASE, "a1")
    await _accept(session, world, borya, p, BASE + timedelta(minutes=5), "b1")
    await _accept(session, world, vova, p, BASE + timedelta(minutes=10), "v1")

    feed = await build_feed(session, anya)
    assert {item.user.display_name for item in feed} == {"Аня", "Боря"}
    # Свежие события сверху.
    assert [item.user.display_name for item in feed] == ["Боря", "Аня"]


async def test_teacher_sees_everyone(session, world):
    p = world["problems"][0]
    await _accept(session, world, world["anya"], p, BASE, "a1")
    await _accept(session, world, world["vova"], p, BASE + timedelta(minutes=1), "v1")

    feed = await build_feed(session, world["teacher"])
    assert {item.user.display_name for item in feed} == {"Аня", "Вова"}


async def test_group_filter(session, world):
    p = world["problems"][0]
    await _accept(session, world, world["anya"], p, BASE, "a1")
    await _accept(session, world, world["vova"], p, BASE, "v1")

    feed = await build_feed(session, world["teacher"], group_id=world["other"].id)
    assert [item.user.display_name for item in feed] == ["Вова"]


async def test_only_first_accept_per_problem(session, world):
    """Перерешивания не должны засорять ленту."""
    anya, p = world["anya"], world["problems"][0]
    await _accept(session, world, anya, p, BASE, "first")
    await _accept(session, world, anya, p, BASE + timedelta(days=1), "again")
    await _accept(session, world, anya, p, BASE + timedelta(days=2), "and-again")

    feed = await build_feed(session, anya)
    assert len(feed) == 1
    assert feed[0].solved_at == BASE


async def test_failed_submissions_never_appear(session, world):
    anya = world["anya"]
    session.add(Submission(
        user_id=anya.id, platform_account_id=world["accounts"][anya.id].id,
        platform=Platform.leetcode, external_id="wa1", problem_id=world["problems"][0].id,
        problem_slug="two-sum", verdict="Wrong Answer", is_accepted=False, submitted_at=BASE,
    ))
    await session.commit()
    assert await build_feed(session, anya) == []


async def test_item_is_tagged_with_assignment(session, world):
    anya, problem = world["anya"], world["problems"][0]

    problem_set = ProblemSet(title="Список")
    session.add(problem_set)
    await session.commit()
    session.add(ProblemSetItem(problem_set_id=problem_set.id, problem_id=problem.id))
    session.add(Assignment(title="Неделя 1", problem_set_id=problem_set.id,
                           group_id=world["group"].id, assigned_at=BASE))
    await session.commit()

    await _accept(session, world, anya, problem, BASE + timedelta(hours=2), "a1")
    feed = await build_feed(session, anya)
    assert feed[0].assignment is not None
    assert feed[0].assignment.title == "Неделя 1"


async def test_solve_before_assignment_is_not_tagged(session, world):
    anya, problem = world["anya"], world["problems"][0]

    problem_set = ProblemSet(title="Список")
    session.add(problem_set)
    await session.commit()
    session.add(ProblemSetItem(problem_set_id=problem_set.id, problem_id=problem.id))
    session.add(Assignment(title="Неделя 1", problem_set_id=problem_set.id,
                           group_id=world["group"].id, assigned_at=BASE))
    await session.commit()

    await _accept(session, world, anya, problem, BASE - timedelta(days=3), "old")
    feed = await build_feed(session, anya)
    assert feed[0].assignment is None


async def test_difficulty_badge(session, world):
    anya = world["anya"]
    await _accept(session, world, anya, world["problems"][0], BASE, "a1")
    await _accept(session, world, anya, world["problems"][1], BASE + timedelta(minutes=1), "a2")

    feed = await build_feed(session, anya)
    badges = {item.title: item.difficulty_badge for item in feed}
    assert badges["Two Sum"] == ("Easy", "easy")
    assert badges["Watermelon"] == ("800", "rating")


async def test_visible_ids_include_self_without_group(session, world):
    loner = User(display_name="Один")
    session.add(loner)
    await session.commit()
    assert await visible_user_ids(session, loner) == [loner.id]


async def test_feed_page_renders(session, client):
    await client.post("/login/dev", data={"name": "Аня"})
    response = await client.get("/feed")
    assert response.status_code == 200
    assert "Лента решений" in response.text
    assert "Пока тихо" in response.text


async def test_only_user_narrows_feed_to_one_person(session, world):
    """Лента профиля показывает решения только его владельца."""
    p = world["problems"][0]
    await _accept(session, world, world["anya"], p, BASE, "a1")
    await _accept(session, world, world["borya"], p, BASE + timedelta(minutes=1), "b1")

    feed = await build_feed(session, world["anya"], only_user=world["borya"])
    assert [item.user.display_name for item in feed] == ["Боря"]


async def test_codeforces_item_links_to_the_public_submission(session, world):
    """Кода решения у нас нет, но у Codeforces страница посылки открыта всем."""
    anya, codeforces = world["anya"], world["problems"][1]
    await _accept(session, world, anya, codeforces, BASE, "312456789")

    item = (await build_feed(session, anya))[0]
    assert item.submission_url == "https://codeforces.com/contest/4/submission/312456789"


async def test_leetcode_item_has_no_submission_link(session, world):
    """У LeetCode публичной страницы посылки нет — ссылке в никуда не место."""
    anya, leetcode = world["anya"], world["problems"][0]
    await _accept(session, world, anya, leetcode, BASE, "9911")

    item = (await build_feed(session, anya))[0]
    assert item.submission_url is None


async def test_teachers_never_appear_in_the_feed(session, world):
    """Лента про то, как идёт группа: решения преподавателя сбивают эту картину."""
    teacher, problem = world["teacher"], world["problems"][0]
    account = PlatformAccount(
        user_id=teacher.id, platform=Platform.leetcode, handle="teacher", verified_at=BASE
    )
    session.add(account)
    await session.commit()
    session.add(Submission(
        user_id=teacher.id, platform_account_id=account.id, platform=problem.platform,
        external_id="t1", problem_id=problem.id, problem_slug=problem.slug,
        problem_title=problem.title, verdict="Accepted", is_accepted=True, submitted_at=BASE,
    ))
    await session.commit()

    # Ни студенту, ни самому преподавателю.
    assert all(item.user.id != teacher.id for item in await build_feed(session, world["anya"]))
    assert all(item.user.id != teacher.id for item in await build_feed(session, teacher))
