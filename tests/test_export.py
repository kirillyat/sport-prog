"""Выгрузка в CSV: итоги табло и матрица по заданию."""

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
    Role,
    Submission,
    User,
)

BASE = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def decode(body: bytes) -> list[list[str]]:
    """Читаем так же, как прочитает Excel: с BOM и точкой с запятой."""
    text = body.decode("utf-8-sig")
    return [line.split(";") for line in text.strip().split("\r\n")]


@pytest.fixture
async def world(session, client):
    await client.post("/login/dev", data={"name": "Кирилл", "teacher": "true"})
    group = Group(title="Осень", join_code="EXP123")
    anya = User(display_name="Аня", role=Role.student, oidc_sub="ak-1")
    borya = User(display_name="Боря", role=Role.student)
    problems = [
        Problem(platform=Platform.leetcode, external_id=str(i), slug=f"p{i}",
                title=f"Задача {i}", url="", difficulty="Easy")
        for i in (1, 2)
    ]
    problem_set = ProblemSet(title="Список")
    session.add_all([group, anya, borya, problem_set, *problems])
    await session.commit()

    session.add_all([
        GroupMembership(group_id=group.id, user_id=anya.id),
        GroupMembership(group_id=group.id, user_id=borya.id),
    ])
    session.add_all([
        ProblemSetItem(problem_set_id=problem_set.id, problem_id=p.id, position=i)
        for i, p in enumerate(problems)
    ])
    assignment = Assignment(
        title="Неделя 1", problem_set_id=problem_set.id, group_id=group.id,
        assigned_at=BASE, deadline=BASE + timedelta(days=7),
    )
    session.add(assignment)
    await session.commit()

    account = PlatformAccount(user_id=anya.id, platform=Platform.leetcode,
                              handle="anya", verified_at=BASE)
    session.add(account)
    await session.commit()
    # Аня: одна в срок, одна с опозданием. Боря не решал.
    session.add_all([
        Submission(user_id=anya.id, platform_account_id=account.id, platform=Platform.leetcode,
                   external_id="s1", problem_id=problems[0].id, problem_slug=problems[0].slug,
                   verdict="Accepted", is_accepted=True, submitted_at=BASE + timedelta(hours=2)),
        Submission(user_id=anya.id, platform_account_id=account.id, platform=Platform.leetcode,
                   external_id="s2", problem_id=problems[1].id, problem_slug=problems[1].slug,
                   verdict="Accepted", is_accepted=True, submitted_at=BASE + timedelta(days=9)),
    ])
    await session.commit()
    return {"assignment": assignment, "group": group, "anya": anya, "borya": borya}


async def test_leaderboard_export_has_a_row_per_student(client, world):
    response = await client.get(f"/teacher/export/leaderboard.csv?group_id={world['group'].id}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert response.content.startswith(b"\xef\xbb\xbf")      # BOM для Excel

    rows = decode(response.content)
    assert rows[0][:6] == ["Место", "Студент", "Группы", "Зачтено", "Выдано", "Доля, %"]
    by_name = {row[1]: row for row in rows[1:]}
    assert by_name["Аня"][2] == "Осень"
    assert by_name["Аня"][3:7] == ["1", "2", "50", "1"]      # зачтено, выдано, доля, опоздания
    assert by_name["Аня"][9] == "да"                         # подтверждён через вуз
    assert by_name["Боря"][3] == "0"
    assert by_name["Боря"][9] == "нет"


async def test_leaderboard_export_is_always_about_one_group(client, world, session):
    """Общего табло нет, поэтому и выгрузки «по всем» тоже: без группы — отказ."""
    outsider = User(display_name="Вова", role=Role.student)
    session.add(outsider)
    await session.commit()

    without_group = await client.get("/teacher/export/leaderboard.csv")
    assert not without_group.headers["content-type"].startswith("text/csv")
    assert "Выбери группу" in without_group.text

    group_id = world["group"].id
    narrowed = await client.get(f"/teacher/export/leaderboard.csv?group_id={group_id}")
    assert {row[1] for row in decode(narrowed.content)[1:]} == {"Аня", "Боря"}


async def test_assignment_export_is_a_matrix(client, world):
    assignment_id = world["assignment"].id
    response = await client.get(f"/teacher/assignments/{assignment_id}/export.csv")
    assert response.status_code == 200

    rows = decode(response.content)
    assert rows[0] == ["Студент", "Зачтено", "Всего задач", "1. Задача 1", "2. Задача 2"]
    by_name = {row[0]: row for row in rows[1:]}
    assert by_name["Аня"][1:] == ["2", "2", "в срок", "после дедлайна"]
    assert by_name["Боря"][1:] == ["0", "2", "", ""]


async def test_students_cannot_export(session, client, world):
    await client.post("/logout")
    await client.post("/login/dev", data={"name": "Аня"})

    for path in ("/teacher/export/leaderboard.csv", "/teacher/assignments/1/export.csv"):
        assert (await client.get(path)).status_code == 403, path


async def test_export_button_is_shown_to_the_teacher_only(client, world):
    assert "export/leaderboard.csv" in (await client.get("/leaderboard")).text

    await client.post("/logout")
    await client.post("/login/dev", data={"name": "Аня"})
    assert "export/leaderboard.csv" not in (await client.get("/leaderboard")).text


async def test_missing_assignment_does_not_crash_the_export(client, world):
    response = await client.get("/teacher/assignments/999/export.csv")
    assert "не найдено" in response.text
