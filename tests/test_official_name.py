"""Два имени: из учётной записи вуза — для ведомости, на портале — какое захочется."""

from __future__ import annotations

import csv
import io
from datetime import timedelta

from app.models import (
    Assignment,
    Group,
    GroupMembership,
    Platform,
    Problem,
    ProblemSet,
    ProblemSetItem,
    Role,
    User,
    utcnow,
)
from app.routers.auth import _resolve_oidc_user


async def _teacher(session) -> User:
    # Первый вошедший становится преподавателем — пусть им будет преподаватель.
    teacher = User(display_name="Кирилл", role=Role.teacher)
    session.add(teacher)
    await session.commit()
    return teacher


async def _renamed_student(session, client) -> User:
    student = await _resolve_oidc_user(session, {"sub": "ak-7", "name": "Иванов Иван"}, set())
    await client.post("/login/dev", data={"name": "Иванов Иван"})
    await client.post(f"/u/{student.id}/name", data={"display_name": "Гэндальф"})
    await client.post("/logout")
    await session.refresh(student)
    return student


async def test_the_university_name_survives_a_rename_and_follows_the_account(session, client):
    await _teacher(session)
    student = await _renamed_student(session, client)
    assert (student.display_name, student.official_name) == ("Гэндальф", "Иванов Иван")

    # В учётной записи поправили ФИО — портал узнает при следующем входе,
    # а имя, которое студент выбрал себе сам, вход не трогает.
    again = await _resolve_oidc_user(
        session, {"sub": "ak-7", "name": "Иванов Иван Петрович"}, set()
    )
    assert (again.display_name, again.official_name) == ("Гэндальф", "Иванов Иван Петрович")


async def test_a_login_without_a_real_name_leaves_the_official_name_empty(session):
    """Логин и почта — не имя: по ним ведомость не заполнишь."""
    await _teacher(session)
    user = await _resolve_oidc_user(session, {"sub": "ak-8", "preferred_username": "ivanov"}, set())
    assert (user.display_name, user.official_name) == ("ivanov", None)
    assert user.record_name == "ivanov" and not user.renamed


async def test_teacher_sees_both_names_and_classmates_only_the_nick(session, client):
    await _teacher(session)
    student = await _renamed_student(session, client)
    session.add(User(display_name="Аня"))
    await session.commit()

    await client.post("/login/dev", data={"name": "Кирилл"})
    page = (await client.get("/teacher/students")).text
    assert "Иванов Иван" in page and "Гэндальф" in page
    await client.post("/logout")

    await client.post("/login/dev", data={"name": "Аня"})
    profile = (await client.get(f"/u/{student.id}")).text
    assert "Гэндальф" in profile and "Иванов Иван" not in profile
    await client.post("/logout")

    # Сам студент знает, что преподаватель видит имя из учётной записи.
    await client.post("/login/dev", data={"name": "Гэндальф"})
    own = (await client.get(f"/u/{student.id}")).text
    assert "Иванов Иван" in own


async def test_the_assignment_export_goes_by_the_university_name(session, client):
    await _teacher(session)
    student = await _renamed_student(session, client)
    problem = Problem(platform=Platform.leetcode, external_id="1", slug="two-sum",
                      title="Two Sum", url="https://leetcode.com/problems/two-sum/")
    group = Group(title="ПМИ-1", join_code="PMI1")
    problem_set = ProblemSet(title="Список")
    session.add_all([problem, group, problem_set])
    await session.commit()
    session.add_all([
        GroupMembership(group_id=group.id, user_id=student.id),
        ProblemSetItem(problem_set_id=problem_set.id, problem_id=problem.id),
    ])
    assignment = Assignment(title="ДЗ", problem_set_id=problem_set.id, group_id=group.id,
                            assigned_at=utcnow() - timedelta(days=1))
    session.add(assignment)
    await session.commit()

    await client.post("/login/dev", data={"name": "Кирилл"})
    response = await client.get(f"/teacher/assignments/{assignment.id}/export.csv")
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig")), delimiter=";"))
    assert rows[1][:2] == ["Иванов Иван", "Гэндальф"]
