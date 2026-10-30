"""Соревнования: регистрация команд, старт по кнопке, табло ICPC и IOI.

Судью подменяем, как в задачах: проверяем правила портала — задачи до
старта закрыты, штраф считается по ICPC, заморозка прячет результат, а
посылки соревнования не попадают в учёбу.
"""

from __future__ import annotations

import io
import zipfile
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import (
    Contest,
    ContestRules,
    ContestRun,
    FeatureFlag,
    Role,
    Submission,
    User,
    utcnow,
)
from app.services import contests, judge, tasks


def _archive(*slugs: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for slug in slugs:
            statement = f"---\ntitle: {slug}\n---\n\nУсловие {slug}.\n"
            archive.writestr(f"{slug}/statement.md", statement)
            archive.writestr(f"{slug}/tests/open/01.in", "1\n")
            archive.writestr(f"{slug}/tests/open/01.out", "1\n")
            archive.writestr(f"{slug}/tests/closed/01.in", "2\n")
            archive.writestr(f"{slug}/tests/closed/01.out", "2\n")
    return buffer.getvalue()


def _verdict(*passed: bool, status: str = "Wrong Answer"):
    """Судья, который на каждый тест отвечает заранее заданным результатом."""

    async def fake_run(cfg, code, tests, time_limit_ms, memory_limit_mb, language_id=None):
        return judge.RunResult(
            results=[
                judge.TestResult(
                    position=t.position,
                    is_open=t.is_open,
                    passed=ok,
                    status="Accepted" if ok else status,
                )
                for t, ok in zip(tests, passed, strict=False)
            ]
        )

    return fake_run


async def _user(session, name, teacher=False) -> User:
    user = User(display_name=name, role=Role.teacher if teacher else Role.student)
    session.add(user)
    await session.commit()
    return user


@pytest.fixture
async def problems(session):
    return await tasks.save(session, tasks.parse_archive(_archive("alpha", "beta")), author_id=None)


@pytest.fixture
async def teacher(session):
    return await _user(session, "Кирилл", teacher=True)


@pytest.fixture
async def contest(session, teacher, problems):
    item = await contests.create(session, teacher, title="Осенний тур", team_size=3)
    await contests.set_problems(session, item, ["alpha", "beta"])
    await contests.publish(session, item)
    return item


async def _login(client, name, teacher=False):
    data = {"name": name}
    if teacher:
        data["teacher"] = "true"
    await client.post("/login/dev", data=data)


async def _open_section(session):
    session.add(FeatureFlag(key="contest", for_students=True, for_teachers=True))
    await session.commit()


def _run(contest, team, user, problem, minute, *, accepted=False, passed=0, total=2, ce=False):
    return ContestRun(
        contest_id=contest.id,
        team_id=team.id,
        user_id=user.id,
        problem_id=problem.id,
        code="print()",
        verdict="Принято" if accepted else "WA",
        accepted=accepted,
        compile_error=ce,
        passed_tests=passed,
        total_tests=total,
        submitted_at=contest.started_at + timedelta(minutes=minute, seconds=10),
    )


# ------------------------------------------------------------------ доступ


async def test_contest_section_is_closed_to_students_until_teacher_opens_it(
    session, client, contest
):
    """Раздел обкатывается: без явного флага студенты его не видят вовсе."""
    await _login(client, "Кирилл", teacher=True)
    assert (await client.get("/contest")).status_code == 200
    await client.post("/logout")

    await _login(client, "Аня")
    assert (await client.get("/contest")).status_code == 403
    assert 'href="/contest"' not in (await client.get("/")).text

    await _open_section(session)
    page = await client.get("/contest")
    assert page.status_code == 200 and "Осенний тур" in page.text


async def test_problems_stay_closed_until_the_start_button(session, client, contest, problems):
    await _login(client, "Кирилл", teacher=True)
    await client.post("/logout")
    await _open_section(session)
    await _login(client, "Аня")

    before = await client.get(f"/contest/{contest.id}/problems/A")
    assert "Условие alpha" not in before.text
    # Слаг из архива легко угадать — по общему адресу задача тоже закрыта.
    assert "Условие alpha" not in (await client.get("/tasks/alpha")).text

    await contests.start(session, contest)
    after = await client.get(f"/contest/{contest.id}/problems/A")
    assert "Условие alpha" in after.text
    # Задача соревнования живёт в разделе — и после старта, и после конца.
    assert "Условие alpha" not in (await client.get("/tasks/alpha")).text


async def test_draft_is_invisible_to_students(session, teacher, problems):
    draft = await contests.create(session, teacher, title="Черновик")
    student = await _user(session, "Аня")
    assert draft not in await contests.visible(session, student)
    assert draft in await contests.visible(session, teacher)


# ------------------------------------------------------------------ команды


async def test_team_fills_up_to_its_size_and_one_person_is_in_one_team(session, contest):
    contest.team_size = 2
    captain, mate, extra = [await _user(session, n) for n in ("Аня", "Боря", "Вера")]
    team = await contests.create_team(session, contest, captain, "Три сеньора")
    await contests.join_team(session, contest, mate, team.join_code)

    with pytest.raises(contests.ContestError, match="полная"):
        await contests.join_team(session, contest, extra, team.join_code)
    with pytest.raises(contests.ContestError, match="уже в команде"):
        await contests.create_team(session, contest, mate, "Ещё одна")


async def test_individual_contest_registers_a_team_of_one_named_after_the_person(
    session, teacher, problems
):
    solo = await contests.create(session, teacher, title="Личный", team_size=1)
    await contests.publish(session, solo)
    anya = await _user(session, "Аня")
    team = await contests.create_team(session, solo, anya, "что угодно")
    assert team.name == "Аня" and len(team.members) == 1


async def test_captain_leaving_hands_the_team_to_the_next_member(session, contest):
    captain, mate = await _user(session, "Аня"), await _user(session, "Боря")
    team = await contests.create_team(session, contest, captain, "Команда")
    await contests.join_team(session, contest, mate, team.join_code)

    await contests.leave_team(session, contest, captain)
    await session.refresh(team)
    assert team.captain_id == mate.id

    await contests.leave_team(session, contest, mate)
    assert await contests.teams(session, contest) == []


# ------------------------------------------------------------------ табло


async def test_icpc_penalty_is_solve_minute_plus_twenty_per_failed_try(
    session, contest, problems
):
    await contests.start(session, contest)
    anya, borya = await _user(session, "Аня"), await _user(session, "Боря")
    first = await contests.create_team(session, contest, anya, "Первая")
    second = await contests.create_team(session, contest, borya, "Вторая")
    alpha, beta = problems
    session.add_all([
        _run(contest, first, anya, alpha, 5),
        _run(contest, first, anya, alpha, 7, ce=True),          # компиляция не штрафуется
        _run(contest, first, anya, alpha, 10, accepted=True, passed=2),
        _run(contest, first, anya, alpha, 12),                  # после принятой — не считается
        _run(contest, second, borya, alpha, 15, accepted=True, passed=2),
        _run(contest, second, borya, beta, 40, accepted=True, passed=2),
    ])
    await session.commit()

    board = await contests.standings(session, contest)
    top, below = board.rows
    assert (top.team.name, top.solved, top.penalty) == ("Вторая", 2, 55)
    assert (below.team.name, below.solved, below.penalty) == ("Первая", 1, 30)
    assert below.cell(alpha.id).tries == 1 and below.cell(alpha.id).first


async def test_ioi_takes_the_best_run_per_problem(session, contest, problems):
    contest.rules = ContestRules.ioi
    await contests.start(session, contest)
    anya = await _user(session, "Аня")
    team = await contests.create_team(session, contest, anya, "Одна")
    alpha, _beta = problems
    session.add_all([
        _run(contest, team, anya, alpha, 5, passed=2, total=4),
        _run(contest, team, anya, alpha, 9, passed=1, total=4),  # хуже — не отнимает баллы
    ])
    await session.commit()

    row = (await contests.standings(session, contest)).rows[0]
    assert row.score == 50 and row.cell(alpha.id).minute == 5


async def test_freeze_hides_late_runs_from_participants_but_not_from_teacher(
    session, contest, problems
):
    contest.freeze_min = 60
    await contests.start(session, contest)
    contest.started_at = utcnow() - timedelta(minutes=250)
    contest.ends_at = contest.started_at + timedelta(minutes=300)
    anya = await _user(session, "Аня")
    team = await contests.create_team(session, contest, anya, "Одна")
    alpha, _beta = problems
    session.add(_run(contest, team, anya, alpha, 245, accepted=True, passed=2))
    await session.commit()

    frozen = await contests.standings(session, contest)
    assert frozen.frozen and frozen.rows[0].solved == 0
    assert frozen.rows[0].cell(alpha.id).pending == 1

    live = await contests.standings(session, contest, live=True)
    assert live.rows[0].solved == 1

    await contests.finish(session, contest)
    assert (await contests.standings(session, contest)).rows[0].solved == 1


# ------------------------------------------------------------------ сдача


async def test_contest_run_stays_out_of_course_submissions(session, client, contest, monkeypatch):
    """Посылка тура не должна закрыть домашку и всплыть в ленте курса."""
    await _login(client, "Кирилл", teacher=True)
    await client.post("/logout")
    await _open_section(session)
    await judge.connect(session, "http://judge.test")
    monkeypatch.setattr(judge, "run", _verdict(True, True))
    await contests.start(session, contest)

    await _login(client, "Аня")
    await client.post(f"/contest/{contest.id}/team", data={"name": "Одна"})
    page = await client.post(
        f"/contest/{contest.id}/problems/A/submit", data={"code": "print(input())"}
    )

    assert "Принято" in page.text
    run = await session.scalar(select(ContestRun))
    assert run.accepted and not run.practice and run.team_id is not None
    assert await session.scalar(select(Submission)) is None


async def test_run_after_the_end_is_practice_and_leaves_the_board_alone(
    session, contest, problems
):
    await contests.start(session, contest)
    anya = await _user(session, "Аня")
    team = await contests.create_team(session, contest, anya, "Одна")
    await contests.finish(session, contest)

    passed = await _verdict(True, True)(None, "", [judge.Case(0, True, "1", "1")] * 2, 1, 1)
    run = await contests.record_run(
        session, contest, user=anya, team=team, problem=problems[0],
        code="print()", language="Python 3", result=passed,
    )
    assert run.practice
    assert (await contests.standings(session, contest)).rows[0].solved == 0


async def test_submission_without_a_team_during_the_round_is_refused(
    session, client, contest, monkeypatch
):
    await _login(client, "Кирилл", teacher=True)
    await client.post("/logout")
    await _open_section(session)
    await contests.start(session, contest)

    await _login(client, "Аня")
    page = await client.post(f"/contest/{contest.id}/problems/A/submit", data={"code": "x=1"})
    assert "зарегистрируйся" in page.text
    # Код не теряется: он возвращается в редактор вместе с отказом.
    assert "x=1" in page.text
    assert await session.scalar(select(ContestRun)) is None


async def test_other_teams_code_is_not_readable(session, client, contest, problems):
    await _login(client, "Кирилл", teacher=True)
    await client.post("/logout")
    await _open_section(session)
    await contests.start(session, contest)
    borya = await _user(session, "Боря")
    other = await contests.create_team(session, contest, borya, "Чужие")
    run = _run(contest, other, borya, problems[0], 3)
    run.code = "секретное_решение = 1"
    session.add(run)
    await session.commit()

    await _login(client, "Аня")
    page = await client.get(f"/contest/{contest.id}/runs/{run.id}")
    assert "секретное_решение" not in page.text


async def test_start_needs_problems(session, teacher):
    empty = await contests.create(session, teacher, title="Пусто")
    with pytest.raises(contests.ContestError, match="задачи"):
        await contests.start(session, empty)
    assert (await session.get(Contest, empty.id)).started_at is None


# ------------------------------------------------------------------ MCP


async def _call(client, key, name, **arguments) -> dict:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}
    response = await client.post("/mcp", json=body, headers={"Authorization": f"Bearer {key}"})
    result = response.json()["result"]
    if result.get("isError"):
        return {"error": result["content"][0]["text"]}
    return result["structuredContent"]


async def test_teacher_sets_up_a_contest_through_mcp_like_on_the_page(
    session, client, teacher, problems
):
    from app.services import api_tokens

    _, key = await api_tokens.issue(session, teacher, "тест", can_write=True)
    created = await _call(
        client, key, "create_contest",
        title="Тур по MCP", team_size=1, problems=["task:beta", "alpha"],
    )
    assert created["phase"] == "draft"
    assert [p["letter"] + p["title"] for p in created["problems"]] == ["Abeta", "Balpha"]

    refused = await _call(client, key, "update_contest", contest_id=created["id"],
                          title="Тур", problems=["two-sum"])
    assert "two-sum" in refused["error"]

    started = await _call(client, key, "start_contest", contest_id=created["id"])
    assert started["phase"] == "running"
    late = await _call(client, key, "update_contest", contest_id=created["id"],
                       title="Тур", problems=["alpha"])
    assert "После старта" in late["error"]


async def test_student_key_gets_no_contest_tools_while_the_section_is_closed(
    session, client, contest
):
    from app.services import api_tokens

    anya = await _user(session, "Аня")
    _, key = await api_tokens.issue(session, anya, "тест", can_write=False)
    closed = await _call(client, key, "list_contests")
    assert "error" in closed

    await _open_section(session)
    opened = await _call(client, key, "list_contests")
    assert [item["title"] for item in opened["contests"]] == ["Осенний тур"]
    # До старта задач нет и в ответе нейросети.
    assert "problems" not in opened["contests"][0]


# ------------------------------------------------------------------ вид


async def test_teacher_sees_a_section_closed_to_students_dashed(session, client, contest):
    """Пунктир в рейке — знак «студенту не видно»: раздел ещё закрыт флагом."""
    await _login(client, "Кирилл", teacher=True)
    closed = await client.get("/teacher")
    assert "staff-only" in _nav_item(closed.text, "/contest")

    await _open_section(session)
    opened = await client.get("/teacher")
    assert "staff-only" not in _nav_item(opened.text, "/contest")


def _nav_item(html: str, href: str) -> str:
    """Тег ссылки рейки на адрес — без содержимого."""
    start = html.index(f'<a href="{href}"')
    return html[start:html.index(">", start)]


async def test_running_contest_is_on_top_of_the_section_page(session, client, contest):
    await _login(client, "Кирилл", teacher=True)
    await client.post("/logout")
    await _open_section(session)
    await contests.start(session, contest)
    anya = await _user(session, "Аня")
    await contests.create_team(session, contest, anya, "Одна")

    await _login(client, "Аня")
    page = (await client.get("/contest")).text
    assert "Твоя команда «Одна»" in page
    assert f'href="/contest/{contest.id}">' in page and "К задачам" in page


async def test_start_without_problems_says_why(session, client, teacher):
    await _login(client, "Кирилл", teacher=True)
    empty = await contests.create(session, teacher, title="Пусто")
    await contests.publish(session, empty)
    page = await client.post(f"/contest/{empty.id}/start")
    assert "сначала задайте задачи" in page.text


def test_contest_mark_never_replaces_the_institution_mark(tmp_path, monkeypatch):
    """Свой знак учреждения без варианта для соревнований остаётся и там:
    запасной нейтральный знак его не подменяет."""
    from app import templating

    monkeypatch.setattr(templating, "BRANDING_DIR", tmp_path)
    assert templating._contest_mark() == "/static/mark-contest.svg"

    (tmp_path / "mark.svg").write_text("<svg/>")
    assert templating._contest_mark() is None

    (tmp_path / "mark-contest.svg").write_text("<svg/>")
    assert templating._contest_mark() == "/branding/mark-contest.svg"
