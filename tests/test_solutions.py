"""Сдача решения файлом и его проверка преподавателем.

Смысл механики: у LeetCode исходник посылки портал получить не может, поэтому
там, где преподавателю нужен текст решения, его присылает сам студент.
Отклонённое решение снимает зачёт — иначе проверка была бы отметкой без веса.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import (
    Assignment,
    Group,
    GroupMembership,
    Platform,
    PlatformAccount,
    Problem,
    ProblemSet,
    ProblemSetItem,
    ReviewStatus,
    Role,
    SolutionUpload,
    Submission,
    User,
)
from app.services import solutions
from app.services.progress import compute_progress

BASE = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(solutions.settings, "data_dir", tmp_path)
    return tmp_path / "solutions"


@pytest.fixture
async def world(session):
    teacher = User(display_name="Кирилл", role=Role.teacher, oidc_sub="t-1")
    anya = User(display_name="Аня", oidc_sub="a-1")
    session.add_all([teacher, anya])
    await session.commit()

    group = Group(title="Осень", join_code="AAA111")
    session.add(group)
    await session.commit()
    session.add(GroupMembership(group_id=group.id, user_id=anya.id))

    problem = Problem(platform=Platform.leetcode, external_id="1", slug="two-sum",
                      title="Two Sum", url="https://leetcode.com/problems/two-sum/",
                      difficulty="Easy")
    session.add(problem)
    await session.commit()

    problem_set = ProblemSet(title="Разминка")
    session.add(problem_set)
    await session.commit()
    session.add(ProblemSetItem(problem_set_id=problem_set.id, problem_id=problem.id, position=0))

    account = PlatformAccount(user_id=anya.id, platform=Platform.leetcode,
                              handle="anya", verified_at=BASE)
    session.add(account)
    await session.commit()

    assignment = Assignment(title="Неделя 1", problem_set_id=problem_set.id, group_id=group.id,
                            assigned_at=BASE, requires_solution=True)
    session.add(assignment)
    await session.commit()

    # Задача решена на платформе — зачёт есть, пока решение не отклонили.
    session.add(Submission(
        user_id=anya.id, platform_account_id=account.id, platform=Platform.leetcode,
        external_id="s1", problem_id=problem.id, problem_slug=problem.slug,
        verdict="Accepted", is_accepted=True, submitted_at=BASE + timedelta(hours=1),
    ))
    await session.commit()
    return {"teacher": teacher, "anya": anya, "assignment": assignment, "problem": problem}


async def _login(client, name, teacher=False):
    data = {"name": name}
    if teacher:
        data["teacher"] = "true"
    await client.post("/login/dev", data=data)


def _file(name="solution.py", content=b"print(1)\n"):
    return {"file": (name, content, "text/plain")}


async def _send(client, world, **kw):
    return await client.post(
        f"/assignments/{world['assignment'].id}/solutions/{world['problem'].id}",
        files=_file(**kw),
    )


async def test_student_sends_a_solution_and_it_waits_for_review(session, client, world):
    await _login(client, "Аня")
    response = await _send(client, world)
    assert "отправлено на проверку" in response.text

    upload = await session.scalar(select(SolutionUpload))
    assert upload.status == ReviewStatus.pending
    assert upload.filename == "solution.py"
    assert solutions.path_for(upload.stored_name).is_file()


async def test_rejected_solution_removes_the_credit(session, client, world):
    """Главное правило: отклонение снимает зачёт, а не просто рисует крестик."""
    await _login(client, "Аня")
    await _send(client, world)
    await client.post("/logout")

    progress = await compute_progress(session, world["assignment"], [world["anya"]])
    assert progress.solved_count(world["anya"].id) == 1      # пока зачёт есть

    await _login(client, "Кирилл", teacher=True)
    upload = await session.scalar(select(SolutionUpload))
    await client.post(f"/solutions/{upload.id}/review",
                      data={"decision": "reject", "comment": "чужой код"})

    progress = await compute_progress(session, world["assignment"], [world["anya"]])
    assert progress.solved_count(world["anya"].id) == 0
    assert progress.cell(world["anya"].id, world["problem"].id).rejected


async def test_accepted_solution_keeps_the_credit(session, client, world):
    await _login(client, "Аня")
    await _send(client, world)
    await client.post("/logout")

    await _login(client, "Кирилл", teacher=True)
    upload = await session.scalar(select(SolutionUpload))
    await client.post(f"/solutions/{upload.id}/review", data={"decision": "accept", "comment": ""})

    progress = await compute_progress(session, world["assignment"], [world["anya"]])
    assert progress.solved_count(world["anya"].id) == 1


async def test_rejecting_without_a_comment_is_refused(session, client, world):
    """Снятие зачёта без объяснения — конфликт со студентом на ровном месте."""
    await _login(client, "Аня")
    await _send(client, world)
    await client.post("/logout")

    await _login(client, "Кирилл", teacher=True)
    upload = await session.scalar(select(SolutionUpload))
    response = await client.post(f"/solutions/{upload.id}/review",
                                 data={"decision": "reject", "comment": "  "})
    assert "без объяснения нельзя" in response.text
    await session.refresh(upload)
    assert upload.status == ReviewStatus.pending


async def test_resending_starts_the_review_over(session, client, world):
    await _login(client, "Аня")
    await _send(client, world)
    await client.post("/logout")

    await _login(client, "Кирилл", teacher=True)
    upload = await session.scalar(select(SolutionUpload))
    await client.post(f"/solutions/{upload.id}/review",
                      data={"decision": "reject", "comment": "перепиши"})
    await client.post("/logout")

    await _login(client, "Аня")
    await _send(client, world, name="fixed.py", content=b"print(2)\n")

    again = await session.scalar(select(SolutionUpload))
    assert again.status == ReviewStatus.pending and again.filename == "fixed.py"
    assert again.comment is None
    # И зачёт вернулся: отклонение было снято новой отправкой.
    progress = await compute_progress(session, world["assignment"], [world["anya"]])
    assert progress.solved_count(world["anya"].id) == 1


async def test_only_allowed_extensions_are_accepted(session, client, world):
    await _login(client, "Аня")
    response = await _send(client, world, name="solution.exe", content=b"MZ")
    assert "не принимаем" in response.text
    assert await session.scalar(select(SolutionUpload)) is None


async def test_assignment_without_the_flag_takes_no_files(session, client, world):
    world["assignment"].requires_solution = False
    await session.commit()

    await _login(client, "Аня")
    response = await _send(client, world)
    assert "решения файлом не требует" in response.text


async def test_student_cannot_read_a_foreign_solution(session, client, world):
    await _login(client, "Аня")
    await _send(client, world)
    upload = await session.scalar(select(SolutionUpload))
    await client.post("/logout")

    borya = User(display_name="Боря", oidc_sub="b-1")
    session.add(borya)
    await session.commit()
    await _login(client, "Боря")

    response = await client.get(f"/solutions/{upload.id}")
    assert "Решение не найдено" in response.text


async def test_teacher_sees_the_queue(session, client, world):
    await _login(client, "Аня")
    await _send(client, world)
    await client.post("/logout")

    await _login(client, "Кирилл", teacher=True)
    page = await client.get("/teacher/reviews")
    assert page.status_code == 200
    assert "Аня" in page.text and "Two Sum" in page.text


async def test_student_cannot_review(session, client, world):
    await _login(client, "Аня")
    await _send(client, world)
    upload = await session.scalar(select(SolutionUpload))

    response = await client.post(f"/solutions/{upload.id}/review",
                                 data={"decision": "accept", "comment": ""})
    assert "может преподаватель" in response.text
    await session.refresh(upload)
    assert upload.status == ReviewStatus.pending


async def test_matrix_shows_who_has_not_sent_a_file(session, client, world):
    """Преподавателю нужен список должников, а не только крестики в клетках."""
    await _login(client, "Кирилл", teacher=True)
    page = (await client.get(f"/teacher/assignments/{world['assignment'].id}")).text
    assert "Решения" in page
    assert "не сдал 1" in page

    await client.post("/logout")
    await _login(client, "Аня")
    await _send(client, world)
    await client.post("/logout")

    await _login(client, "Кирилл", teacher=True)
    page = (await client.get(f"/teacher/assignments/{world['assignment'].id}")).text
    assert "не сдал" not in page          # долг закрыт
    assert "1/1" in page                  # прислано столько же, сколько задач


async def test_matrix_marks_the_problem_where_the_file_is_missing(session, client, world):
    """Счётчик по студенту отвечает «кто», а клетка и столбец — «по какой задаче»."""
    await _login(client, "Кирилл", teacher=True)
    page = (await client.get(f"/teacher/assignments/{world['assignment'].id}")).text
    assert "no-file" in page                  # уголок в клетке
    assert "Сдали файл" in page               # столбец в таблице задач
    assert "0 / 1" in page                    # прислал ноль из одного

    await client.post("/logout")
    await _login(client, "Аня")
    await _send(client, world)
    await client.post("/logout")

    await _login(client, "Кирилл", teacher=True)
    page = (await client.get(f"/teacher/assignments/{world['assignment'].id}")).text
    assert "no-file" not in page
    assert "1 / 1" in page
