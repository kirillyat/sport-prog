"""Свои задачи: набор из архива, страница задачи, сдача и попытки.

Судья в тестах не участвует: его подменяем. Проверяем правила портала —
закрытые тесты не показываются, попытки кончаются, а сдача становится
обычной посылкой, которую видит зачёт.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from sqlalchemy import select

from app.models import Platform, Problem, Submission, Task, TaskTest
from app.services import judge, tasks

STATEMENT = """---
title: Сумма двух
time_limit_ms: 1500
---

Выведите индексы двух чисел.
"""


def _zip(**extra: str) -> bytes:
    files = {
        "two-sum/statement.md": STATEMENT,
        "two-sum/tests/open/01.in": "4\n2 7 11 15\n9\n",
        "two-sum/tests/open/01.out": "0 1\n",
        "two-sum/tests/closed/01.in": "3\n3 2 4\n6\n",
        "two-sum/tests/closed/01.out": "1 2\n",
    }
    files.update(extra)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return buffer.getvalue()


async def _login(client, name, teacher=False):
    data = {"name": name}
    if teacher:
        data["teacher"] = "true"
    await client.post("/login/dev", data=data)


@pytest.fixture
async def task(session):
    parsed = tasks.parse_archive(_zip())
    problems = await tasks.save(session, parsed, author_id=None)
    return problems[0]


def test_archive_reads_title_and_limits():
    task = tasks.parse_archive(_zip())[0]

    assert (task.slug, task.title, task.time_limit_ms) == ("two-sum", "Сумма двух", 1500)
    assert len(task.tests) == 2 and task.open_tests == 1


def test_archive_without_open_tests_is_refused():
    """Без открытого теста студенту не на что опереться — это ошибка набора."""
    files = {"solo/statement.md": "---\ntitle: Одна\n---\n", "solo/tests/closed/01.in": "1\n",
             "solo/tests/closed/01.out": "1\n"}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)

    with pytest.raises(tasks.ArchiveError, match="открыт"):
        tasks.parse_archive(buffer.getvalue())


def test_archive_without_answer_file_is_refused():
    with pytest.raises(tasks.ArchiveError, match="ответ"):
        tasks.parse_archive(_zip(**{"two-sum/tests/open/02.in": "1\n"}))


async def test_upload_replaces_tests_instead_of_adding(session, task):
    """Набор — снимок: пересданный архив не должен удваивать тесты."""
    parsed = tasks.parse_archive(_zip())
    await tasks.save(session, parsed, author_id=None)

    stored = await session.scalar(select(Task).where(Task.problem_id == task.id))
    tests = await tasks.tests_for(session, stored)
    assert len(tests) == 2


async def test_task_page_hides_closed_tests(session, client, task):
    """Закрытый тест не должен утечь в разметку — иначе его подгонят."""
    await _login(client, "Аня")
    page = await client.get(f"/tasks/{task.slug}")

    assert page.status_code == 200
    assert "2 7 11 15" in page.text          # открытый тест виден
    assert "3 2 4" not in page.text          # закрытый — нет


async def test_submit_without_judge_saves_code_and_spends_attempt(session, client, task):
    """Судьи нет — код принимаем, но вердикта не выдумываем."""
    await _login(client, "Аня")
    response = await client.post(f"/tasks/{task.slug}/submit", data={"code": "print(1)"})

    assert "Не проверено" in response.text
    submission = await session.scalar(select(Submission))
    assert submission.platform == Platform.local
    assert submission.code == "print(1)" and submission.is_accepted is False


async def test_attempts_run_out(session, client, task, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "task_attempts", 2)
    await _login(client, "Аня")
    for _ in range(2):
        await client.post(f"/tasks/{task.slug}/submit", data={"code": "print(1)"})

    response = await client.post(f"/tasks/{task.slug}/submit", data={"code": "print(1)"})
    assert "Попытки кончились" in response.text
    assert len((await session.execute(select(Submission))).scalars().all()) == 2


async def test_accepted_submission_counts_as_solved(session, client, task, monkeypatch):
    """Принятая сдача — обычная посылка: её видит и зачёт, и лента."""
    async def fake_run(cfg, code, tests, time_limit_ms, memory_limit_mb):
        return judge.RunResult(
            results=[
                judge.TestResult(
                    position=t.position, is_open=t.is_open, passed=True, status="Accepted"
                )
                for t in tests
            ]
        )

    await judge.connect(session, "http://judge.test")
    monkeypatch.setattr(judge, "run", fake_run)

    await _login(client, "Аня")
    response = await client.post(f"/tasks/{task.slug}/submit", data={"code": "print(1)"})

    assert "принято" in response.text.lower()
    submission = await session.scalar(select(Submission))
    assert submission.is_accepted is True


async def test_running_open_tests_does_not_spend_an_attempt(session, client, task, monkeypatch):
    async def fake_run(cfg, code, tests, time_limit_ms, memory_limit_mb):
        assert all(t.is_open for t in tests), "прогон не должен трогать закрытые тесты"
        return judge.RunResult(
            results=[
                judge.TestResult(position=0, is_open=True, passed=False, status="Wrong Answer")
            ]
        )

    await judge.connect(session, "http://judge.test")
    monkeypatch.setattr(judge, "run", fake_run)

    await _login(client, "Аня")
    response = await client.post(f"/tasks/{task.slug}/run", data={"code": "print(1)"})

    assert "Wrong Answer" in response.text
    assert (await session.execute(select(Submission))).scalars().all() == []


async def test_own_task_goes_into_a_problem_set_by_prefix(session, task):
    """Голый слаг неотличим от LeetCode, поэтому свои задачи пишутся с task:."""
    from app.services.problem_parser import parse_problem_list

    result = await parse_problem_list(session, "task:two-sum")
    assert [p.id for p in result.problems] == [task.id]

    plain = await parse_problem_list(session, "two-sum")
    assert plain.problems == [] and plain.unresolved == ["two-sum"]


async def test_teacher_uploads_a_set(session, client):
    await _login(client, "Кирилл", teacher=True)
    response = await client.post(
        "/teacher/tasks", files={"file": ("set.zip", _zip(), "application/zip")}
    )

    assert "Загружено задач: 1" in response.text
    problem = await session.scalar(select(Problem).where(Problem.platform == Platform.local))
    assert problem.slug == "two-sum"
    assert (await session.scalar(select(TaskTest).where(TaskTest.is_open.is_(False)))) is not None


async def test_own_task_in_an_assignment_opens_on_the_portal(session, client, task):
    """Своя задача — не внешняя ссылка: ни новой вкладки, ни значка «наружу»."""
    from app.models import Assignment, ProblemSet, ProblemSetItem

    problem_set = ProblemSet(title="Контрольная")
    session.add(problem_set)
    await session.commit()
    session.add(ProblemSetItem(problem_set_id=problem_set.id, problem_id=task.id, position=0))
    assignment = Assignment(title="Контрольная", problem_set_id=problem_set.id)
    session.add(assignment)
    await session.commit()

    await _login(client, "Аня")
    page = await client.get(f"/assignments/{assignment.id}")

    assert 'href="/tasks/two-sum"' in page.text
    assert 'href="/tasks/two-sum" target="_blank"' not in page.text


async def test_judge_is_connected_and_disconnected_without_a_restart(session, monkeypatch):
    """Судью поднимают перед контрольной и гасят после: адрес живёт в базе."""
    from app.config import settings

    monkeypatch.setattr(settings, "judge0_url", "http://from-env")
    assert (await judge.config(session)).url == "http://from-env"

    await judge.connect(session, "http://10.0.0.7:2358 ", token="secret")
    config = await judge.config(session)
    assert (config.url, config.token, config.ready) == ("http://10.0.0.7:2358", "secret", True)

    # Отключение должно гасить судью и там, где адрес задан в окружении.
    await judge.disconnect(session)
    assert (await judge.config(session)).ready is False


async def test_teacher_connects_the_judge_from_the_page(session, client, monkeypatch):
    """Подключение — дело преподавателя в интерфейсе, а не правки .env на сервере."""
    async def fake_ping(cfg):
        assert cfg.url == "http://10.0.0.7:2358"
        return "1.13.0"

    monkeypatch.setattr(judge, "ping", fake_ping)
    await _login(client, "Кирилл", teacher=True)

    page = await client.post("/teacher/judge", data={"url": "http://10.0.0.7:2358"})
    assert "1.13.0" in page.text
    assert (await judge.config(session)).url == "http://10.0.0.7:2358"

    page = await client.post("/teacher/judge/off")
    assert (await judge.config(session)).ready is False


async def test_unreachable_judge_keeps_the_address_and_says_so(session, client, monkeypatch):
    """Судья может ещё подниматься — заставлять вводить адрес заново незачем."""
    async def fake_ping(cfg):
        raise judge.JudgeUnavailable("судья недоступен: connect timeout")

    monkeypatch.setattr(judge, "ping", fake_ping)
    await _login(client, "Кирилл", teacher=True)

    page = await client.post("/teacher/judge", data={"url": "http://10.0.0.7:2358"})
    assert "не отвечает" in page.text
    assert (await judge.config(session)).url == "http://10.0.0.7:2358"


async def test_own_task_does_not_ask_for_the_code_twice(session, client, task):
    """Код своей задачи уже в посылке: «код обязателен» не должен его требовать снова."""
    from app.models import Assignment, ProblemSet, ProblemSetItem, Role, User
    from app.services.progress import compute_progress

    student = User(display_name="Аня", role=Role.student, telegram_id=501)
    problem_set = ProblemSet(title="Контрольная")
    session.add_all([student, problem_set])
    await session.commit()
    session.add(ProblemSetItem(problem_set_id=problem_set.id, problem_id=task.id, position=0))
    assignment = Assignment(
        title="Контрольная", problem_set_id=problem_set.id, requires_solution=True
    )
    session.add(assignment)
    await session.commit()

    progress = await compute_progress(session, assignment)
    assert progress.cell(student.id, task.id).code_missing is False


async def test_a_judge_that_went_down_is_visible_without_a_separate_check(
    session, client, task, monkeypatch
):
    """Виртуалку гасят посреди контрольной — портал узнаёт об этом сам, прогоном."""
    async def fallen(cfg, code, tests, time_limit_ms, memory_limit_mb):
        raise judge.JudgeUnavailable("судья недоступен: connect timeout")

    await judge.connect(session, "http://judge.test")
    monkeypatch.setattr(judge, "run", fallen)
    await _login(client, "Аня")

    page = await client.post(f"/tasks/{task.slug}/run", data={"code": "print(1)"})
    assert "недоступна" in page.text

    state = await judge.health(session)
    assert state.known and not state.ok and "timeout" in state.detail

    # Вернувшийся судья отмечается сам — первым же удавшимся прогоном.
    async def alive(cfg, code, tests, time_limit_ms, memory_limit_mb):
        return judge.RunResult(
            results=[judge.TestResult(position=0, is_open=True, passed=True, status="Accepted")]
        )

    monkeypatch.setattr(judge, "run", alive)
    await client.post(f"/tasks/{task.slug}/run", data={"code": "print(1)"})
    assert (await judge.health(session)).ok is True


async def test_judge_is_configured_outside_the_tasks_page(session, client):
    """Задачи заводят заранее, судью подключают в день контрольной — это разные места."""
    await _login(client, "Кирилл", teacher=True)

    tasks_page = await client.get("/teacher/tasks")
    assert 'action="/teacher/judge"' not in tasks_page.text

    own = await client.get("/teacher/judge")
    assert own.status_code == 200 and 'action="/teacher/judge"' in own.text


async def test_the_database_is_released_while_the_judge_thinks(
    session, client, task, monkeypatch
):
    """Судья отвечает секундами. Держать всё это время соединение к базе
    нельзя: пятнадцати таких ожиданий хватало, чтобы у остальных перестали
    открываться страницы."""
    seen = {}

    async def fake_run(cfg, code, tests, time_limit_ms, memory_limit_mb):
        seen["в транзакции"] = session.in_transaction()
        return judge.RunResult(
            results=[judge.TestResult(position=0, is_open=True, passed=True, status="Accepted")]
        )

    await judge.connect(session, "http://judge.test")
    monkeypatch.setattr(judge, "run", fake_run)
    await _login(client, "Аня")

    await client.post(f"/tasks/{task.slug}/run", data={"code": "print(1)"})
    assert seen["в транзакции"] is False


async def test_one_run_at_a_time_per_student():
    """Десять нажатий подряд заняли бы десять воркеров судьи на всю группу."""
    from app.routers.tasks import _Busy, _one_at_a_time

    async with _one_at_a_time(7):
        with pytest.raises(_Busy):
            async with _one_at_a_time(7):
                pass
        # Соседа это не касается.
        async with _one_at_a_time(8):
            pass

    # После выхода очередь свободна.
    async with _one_at_a_time(7):
        pass


async def test_a_huge_paste_is_refused_before_the_judge_sees_it(session, client, task):
    """Мегабайт из буфера обмена уехал бы к судье столько раз, сколько тестов."""
    from app.services import solutions

    await _login(client, "Аня")
    page = await client.post(
        f"/tasks/{task.slug}/submit", data={"code": "x" * (solutions.MAX_CHARS + 1)}
    )

    assert "длиннее" in page.text
    assert (await session.execute(select(Submission))).scalars().all() == []
