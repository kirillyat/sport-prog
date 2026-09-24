"""Сдача кода решения и его проверка преподавателем.

Смысл механики: у LeetCode исходник посылки портал получить не может, поэтому
там, где преподавателю нужен текст решения, его присылает сам студент —
текстом, без файлов. Отклонённое решение снимает зачёт, иначе проверка была бы
отметкой без веса.
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
from app.services.leaderboard import build_leaderboard
from app.services.progress import compute_progress

BASE = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


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
    return {"teacher": teacher, "anya": anya, "group": group,
            "assignment": assignment, "problem": problem}


async def _login(client, name, teacher=False):
    data = {"name": name}
    if teacher:
        data["teacher"] = "true"
    await client.post("/login/dev", data=data)


CODE = "def two_sum(nums, target):\n    return []\n"


async def _send(client, world, code=CODE):
    return await client.post(
        f"/assignments/{world['assignment'].id}/solutions/{world['problem'].id}",
        data={"code": code},
    )


async def test_student_sends_a_solution_and_it_waits_for_review(session, client, world):
    await _login(client, "Аня")
    response = await _send(client, world)
    assert "отправлено на проверку" in response.text

    upload = await session.scalar(select(SolutionUpload))
    assert upload.status == ReviewStatus.pending
    assert upload.code == CODE.strip()


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


async def test_rejection_reaches_the_leaderboard(session, client, world):
    """Матрица и табло обязаны говорить одно и то же — иначе цифрам не верят."""
    await _login(client, "Аня")
    await _send(client, world)
    await client.post("/logout")

    rows = await build_leaderboard(session, group_id=world["group"].id)
    assert [r.solved for r in rows] == [1]

    await _login(client, "Кирилл", teacher=True)
    upload = await session.scalar(select(SolutionUpload))
    await client.post(f"/solutions/{upload.id}/review",
                      data={"decision": "reject", "comment": "чужой код"})

    rows = await build_leaderboard(session, group_id=world["group"].id)
    assert [r.solved for r in rows] == [0]


async def test_without_the_code_there_is_no_credit(session, client, world):
    """Задача решена на площадке, кода нет — зачёта нет ни в матрице, ни на табло."""
    progress = await compute_progress(session, world["assignment"], [world["anya"]])
    assert progress.solved_count(world["anya"].id) == 0
    assert progress.cell(world["anya"].id, world["problem"].id).code_missing

    rows = await build_leaderboard(session, group_id=world["group"].id)
    assert [r.solved for r in rows] == [0]

    await _login(client, "Аня")
    await _send(client, world)
    progress = await compute_progress(session, world["assignment"], [world["anya"]])
    assert progress.solved_count(world["anya"].id) == 1


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
    await _send(client, world, code="def two_sum(nums, target):\n    return [0, 1]\n")

    again = await session.scalar(select(SolutionUpload))
    assert again.status == ReviewStatus.pending and "[0, 1]" in again.code
    assert again.comment is None
    # И зачёт вернулся: отклонение было снято новой отправкой.
    progress = await compute_progress(session, world["assignment"], [world["anya"]])
    assert progress.solved_count(world["anya"].id) == 1


async def test_empty_code_is_refused(session, client, world):
    await _login(client, "Аня")
    response = await _send(client, world, code="   \n  ")
    assert "Пустое решение" in response.text
    assert await session.scalar(select(SolutionUpload)) is None


async def test_too_long_code_is_refused(session, client, world):
    """Потолок нужен, чтобы вместо решения не вставили лог на мегабайт."""
    await _login(client, "Аня")
    response = await _send(client, world, code="x = 1\n" * 20_000)
    assert "длиннее" in response.text
    assert await session.scalar(select(SolutionUpload)) is None


async def test_assignment_without_the_flag_takes_no_code(session, client, world):
    world["assignment"].requires_solution = False
    await session.commit()

    await _login(client, "Аня")
    response = await _send(client, world)
    assert "решения кодом не требует" in response.text


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


async def test_matrix_shows_who_has_not_sent_the_code(session, client, world):
    """Преподавателю нужен список должников, а не только крестики в клетках."""
    await _login(client, "Кирилл", teacher=True)
    page = (await client.get(f"/teacher/assignments/{world['assignment'].id}")).text
    assert "Код" in page
    assert "без кода 1" in page

    await client.post("/logout")
    await _login(client, "Аня")
    await _send(client, world)
    await client.post("/logout")

    await _login(client, "Кирилл", teacher=True)
    page = (await client.get(f"/teacher/assignments/{world['assignment'].id}")).text
    assert "без кода" not in page         # долг закрыт
    assert "1/1" in page                  # прислано столько же, сколько задач


async def test_matrix_marks_the_problem_where_the_code_is_missing(session, client, world):
    """Счётчик по студенту отвечает «кто», а клетка и столбец — «по какой задаче»."""
    await _login(client, "Кирилл", teacher=True)
    page = (await client.get(f"/teacher/assignments/{world['assignment'].id}")).text
    assert "код не сдан" in page              # подпись клетки, а не легенды
    assert "Сдали код" in page                # столбец в таблице задач
    assert "0 / 1" in page                    # прислал ноль из одного

    await client.post("/logout")
    await _login(client, "Аня")
    await _send(client, world)
    await client.post("/logout")

    await _login(client, "Кирилл", teacher=True)
    page = (await client.get(f"/teacher/assignments/{world['assignment'].id}")).text
    assert "код не сдан" not in page
    assert "1 / 1" in page


async def _second_student(session, world, code="print(2)"):
    """Ещё одно решение в очереди — от другого студента к той же задаче."""
    боря = User(display_name="Боря", oidc_sub="b-1")
    session.add(боря)
    await session.commit()
    session.add(GroupMembership(group_id=world["group"].id, user_id=боря.id))
    session.add(
        SolutionUpload(
            assignment_id=world["assignment"].id,
            problem_id=world["problem"].id,
            user_id=боря.id,
            code=code,
            status=ReviewStatus.pending,
            submitted_at=BASE + timedelta(hours=3),
        )
    )
    await session.commit()
    return боря


async def test_after_a_decision_the_next_solution_opens(session, client, world):
    """Проверяют подряд: возвращаться в список ради одного клика — половина работы."""
    await _login(client, "Аня")
    await _send(client, world)
    первое = await session.scalar(select(SolutionUpload))
    await _second_student(session, world)
    второе = await session.scalar(
        select(SolutionUpload).where(SolutionUpload.id != первое.id)
    )

    await _login(client, "Кирилл", teacher=True)
    page = await client.post(
        f"/solutions/{первое.id}/review", data={"decision": "accept", "comment": ""}
    )

    assert "Решение принято" in page.text
    # Открылось следующее в очереди, а не список.
    assert "Боря" in page.text
    assert f"/solutions/{второе.id}/download" in page.text


async def test_the_last_decision_returns_to_the_queue(session, client, world):
    await _login(client, "Аня")
    await _send(client, world)
    upload = await session.scalar(select(SolutionUpload))

    await _login(client, "Кирилл", teacher=True)
    page = await client.post(
        f"/solutions/{upload.id}/review", data={"decision": "accept", "comment": ""}
    )

    assert "Непроверенных решений нет" in page.text


async def test_the_queue_size_is_visible_while_reviewing(session, client, world):
    await _login(client, "Аня")
    await _send(client, world)
    upload = await session.scalar(select(SolutionUpload))
    await _second_student(session, world)

    await _login(client, "Кирилл", teacher=True)
    page = await client.get(f"/solutions/{upload.id}")

    assert "в очереди: 2" in page.text
    assert "Пропустить" in page.text


async def test_a_student_sees_no_queue_on_their_own_solution(session, client, world):
    """Счётчик чужой очереди студенту не показываем — ему он ничего не говорит."""
    await _login(client, "Аня")
    await _send(client, world)
    upload = await session.scalar(select(SolutionUpload))
    await _second_student(session, world)

    page = await client.get(f"/solutions/{upload.id}")

    assert page.status_code == 200
    assert "в очереди" not in page.text
    assert "Пропустить" not in page.text
