"""Портал для нейросетей по MCP: кто что видит, кто что может, и что протокол
понимают клиенты обоих поколений."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models import (
    ApiToken,
    Assignment,
    Group,
    GroupMembership,
    Platform,
    Problem,
    ProblemSet,
    ProblemSetItem,
    Role,
    SolutionUpload,
    Submission,
    Task,
    TaskTest,
    User,
    utcnow,
)
from app.services import api_tokens, features

MODERN = "2026-07-28"


# ---------------------------------------------------------------- данные


async def _user(session, name, role=Role.student) -> User:
    user = User(display_name=name, role=role)
    session.add(user)
    await session.commit()
    return user


async def _key(session, user, can_write=False) -> str:
    _, raw = await api_tokens.issue(session, user, "тест", can_write=can_write)
    return raw


async def _problem(session, slug, platform=Platform.leetcode) -> Problem:
    problem = Problem(
        platform=platform,
        external_id=slug,
        slug=slug,
        title=slug.replace("-", " ").title(),
        url=f"/tasks/{slug}" if platform == Platform.local else f"https://leetcode.com/problems/{slug}/",
        difficulty="Easy",
        tags=["Binary Search"],
    )
    session.add(problem)
    await session.commit()
    return problem


async def _group_with(session, title, *members) -> Group:
    group = Group(title=title, join_code=title.upper()[:6])
    session.add(group)
    await session.commit()
    for member in members:
        session.add(GroupMembership(group_id=group.id, user_id=member.id))
    await session.commit()
    return group


async def _assignment(session, group, *problems, **extra) -> Assignment:
    problem_set = ProblemSet(title=f"Список для {group.title}")
    session.add(problem_set)
    await session.commit()
    for position, problem in enumerate(problems):
        session.add(
            ProblemSetItem(problem_set_id=problem_set.id, problem_id=problem.id, position=position)
        )
    assignment = Assignment(
        title=f"Задание {group.title}",
        problem_set_id=problem_set.id,
        group_id=group.id,
        assigned_at=utcnow() - timedelta(days=1),
        **extra,
    )
    session.add(assignment)
    await session.commit()
    return assignment


# ---------------------------------------------------------------- протокол


async def _rpc(client, key, method, params=None, *, id=1, headers=None):
    body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if id is not None:
        body["id"] = id
    sent = {"Authorization": f"Bearer {key}"} if key else {}
    sent.update(headers or {})
    return await client.post("/mcp", json=body, headers=sent)


async def _call(client, key, name, **arguments) -> dict:
    """Результат tools/call: структура при успехе, текст отказа при isError."""
    response = await _rpc(client, key, "tools/call", {"name": name, "arguments": arguments})
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    if result.get("isError"):
        return {"error": result["content"][0]["text"]}
    return result["structuredContent"]


async def _tool_names(client, key) -> set[str]:
    response = await _rpc(client, key, "tools/list")
    return {item["name"] for item in response.json()["result"]["tools"]}


async def test_request_without_a_key_is_refused(session, client):
    response = await _rpc(client, None, "tools/list")
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")


async def test_site_session_cookie_is_not_a_key(session, client):
    """Иначе любая открытая вкладка с порталом стала бы пропуском для чужого скрипта."""
    await client.post("/login/dev", data={"name": "Кирилл", "teacher": "true"})
    assert (await _rpc(client, None, "tools/list")).status_code == 401


async def test_revoked_key_stops_working(session, client):
    anya = await _user(session, "Аня")
    key = await _key(session, anya)
    assert (await _rpc(client, key, "tools/list")).status_code == 200

    token = await session.scalar(select(ApiToken))
    await api_tokens.revoke(session, anya, token.id)
    assert (await _rpc(client, key, "tools/list")).status_code == 401


async def test_key_is_stored_only_as_a_fingerprint(session, client):
    anya = await _user(session, "Аня")
    key = await _key(session, anya)
    token = await session.scalar(select(ApiToken))
    assert key not in (token.token_hash, token.hint)
    assert key.startswith(token.hint)


async def test_legacy_client_shakes_hands_and_calls_tools(session, client):
    """Клиенты до 2026-07-28 начинают с initialize и ждут версию, которую назвали."""
    anya = await _user(session, "Аня")
    key = await _key(session, anya)

    hello = await _rpc(
        client,
        key,
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}},
    )
    result = hello.json()["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert "tools" in result["capabilities"]
    assert "только для чтения" in result["instructions"] or "студента" in result["instructions"]

    initialized = await _rpc(client, key, "notifications/initialized", id=None)
    assert initialized.status_code == 202

    assert (await _rpc(client, key, "ping")).json()["result"] == {}
    assert "whoami" in await _tool_names(client, key)
    me = await _call(client, key, "whoami")
    assert me["name"] == "Аня" and me["role"] == "student"


async def test_legacy_client_with_unknown_version_gets_the_newest_legacy_one(session, client):
    anya = await _user(session, "Аня")
    key = await _key(session, anya)
    hello = await _rpc(client, key, "initialize", {"protocolVersion": "2024-01-01"})
    assert hello.json()["result"]["protocolVersion"] == "2025-11-25"


def _modern(method: str, name: str | None = None) -> tuple[dict, dict]:
    meta = {"_meta": {"io.modelcontextprotocol/protocolVersion": MODERN}}
    headers = {"MCP-Protocol-Version": MODERN, "Mcp-Method": method}
    if name is not None:
        headers["Mcp-Name"] = name
    return meta, headers


async def test_modern_client_needs_no_handshake(session, client):
    anya = await _user(session, "Аня")
    key = await _key(session, anya)

    meta, headers = _modern("server/discover")
    found = (await _rpc(client, key, "server/discover", meta, headers=headers)).json()["result"]
    assert MODERN in found["supportedVersions"] and "2025-06-18" in found["supportedVersions"]
    assert found["resultType"] == "complete"
    assert found["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "sport-portal"

    meta, headers = _modern("tools/list")
    listed = (await _rpc(client, key, "tools/list", meta, headers=headers)).json()["result"]
    assert listed["cacheScope"] == "private" and listed["ttlMs"] > 0

    meta, headers = _modern("tools/call", "whoami")
    called = await _rpc(
        client, key, "tools/call", {"name": "whoami", "arguments": {}, **meta}, headers=headers
    )
    assert called.json()["result"]["structuredContent"]["name"] == "Аня"


async def test_modern_request_whose_headers_disagree_with_the_body_is_refused(session, client):
    """Прокси маршрутизирует по заголовку, мы исполняем тело — они обязаны совпасть."""
    anya = await _user(session, "Аня")
    key = await _key(session, anya)
    meta, headers = _modern("tools/call", "list_assignments")
    response = await _rpc(
        client, key, "tools/call", {"name": "whoami", "arguments": {}, **meta}, headers=headers
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32020


async def test_modern_header_with_non_ascii_name_is_decoded(session, client):
    anya = await _user(session, "Аня")
    key = await _key(session, anya)
    meta, headers = _modern("tools/call", "=?base64?d2hvYW1p?=")  # «whoami»
    response = await _rpc(
        client, key, "tools/call", {"name": "whoami", "arguments": {}, **meta}, headers=headers
    )
    assert response.status_code == 200


async def test_unsupported_modern_version_names_the_supported_ones(session, client):
    anya = await _user(session, "Аня")
    key = await _key(session, anya)
    params = {"_meta": {"io.modelcontextprotocol/protocolVersion": "2099-01-01"}}
    response = await _rpc(
        client,
        key,
        "tools/list",
        params,
        headers={"MCP-Protocol-Version": "2099-01-01", "Mcp-Method": "tools/list"},
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == -32022 and MODERN in error["data"]["supported"]


async def test_page_from_another_site_cannot_reach_the_endpoint(session, client):
    anya = await _user(session, "Аня")
    key = await _key(session, anya)
    response = await _rpc(client, key, "tools/list", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


async def test_stream_endpoint_is_not_offered(session, client):
    assert (await client.get("/mcp")).status_code == 405


# ---------------------------------------------------------------- студент


async def test_student_sees_only_student_tools(session, client):
    anya = await _user(session, "Аня")
    key = await _key(session, anya)
    names = await _tool_names(client, key)
    assert {"list_assignments", "get_assignment", "get_problem"} <= names
    assert not names & {"get_assignment_solutions", "create_problem_set", "list_groups"}

    # И по прямому имени тоже: чужой инструмент неотличим от несуществующего.
    refused = await _call(client, key, "get_assignment_solutions", assignment_id=1)
    assert "нет" in refused["error"]


async def test_student_cannot_issue_a_key_that_writes(session, client):
    anya = await _user(session, "Аня")
    await api_tokens.issue(session, anya, "x", can_write=True)
    token = await session.scalar(select(ApiToken))
    assert token.can_write is False


async def test_student_sees_own_assignment_but_not_another_group(session, client):
    anya = await _user(session, "Аня")
    boris = await _user(session, "Борис")
    mine = await _assignment(session, await _group_with(session, "ПМИ-1", anya),
                             await _problem(session, "two-sum"))
    theirs = await _assignment(session, await _group_with(session, "ПМИ-2", boris),
                               await _problem(session, "binary-search"))
    key = await _key(session, anya)

    listed = await _call(client, key, "list_assignments")
    assert [a["id"] for a in listed["assignments"]] == [mine.id]

    detail = await _call(client, key, "get_assignment", assignment_id=mine.id)
    assert detail["problems"][0]["ref"] == "lc:two-sum"
    assert detail["problems"][0]["status"] == "not_solved"

    hidden = await _call(client, key, "get_assignment", assignment_id=theirs.id)
    assert "не найдено" in hidden["error"]
    other = await _call(client, key, "get_problem", problem="binary-search")
    assert "нет в твоих заданиях" in other["error"]


async def test_solved_problem_counts_for_the_student_as_on_the_site(session, client):
    anya = await _user(session, "Аня")
    problem = await _problem(session, "two-sum")
    assignment = await _assignment(session, await _group_with(session, "ПМИ-1", anya), problem)
    session.add(
        Submission(
            user_id=anya.id, platform=Platform.leetcode, external_id="1", problem_id=problem.id,
            verdict="Accepted", is_accepted=True, submitted_at=utcnow(),
        )
    )
    await session.commit()
    key = await _key(session, anya)

    listed = await _call(client, key, "list_assignments")
    assert (listed["assignments"][0]["solved"], listed["assignments"][0]["total"]) == (1, 1)
    detail = await _call(client, key, "get_assignment", assignment_id=assignment.id)
    assert detail["problems"][0]["counts"] is True


async def test_student_gets_open_tests_but_never_closed_ones(session, client):
    anya = await _user(session, "Аня")
    problem = await _problem(session, "sum-ab", Platform.local)
    task = Task(problem_id=problem.id, statement="Сложи $a$ и $b$.")
    session.add(task)
    await session.commit()
    session.add_all([
        TaskTest(task_id=task.id, position=1, is_open=True, stdin="1 2\n", expected="3\n"),
        TaskTest(task_id=task.id, position=2, is_open=False, stdin="SECRET-IN\n",
                 expected="SECRET-OUT\n"),
    ])
    await session.commit()
    await _assignment(session, await _group_with(session, "ПМИ-1", anya), problem)
    key = await _key(session, anya)

    response = await _rpc(
        client, key, "tools/call", {"name": "get_problem", "arguments": {"problem": "task:sum-ab"}}
    )
    assert "SECRET" not in response.text
    found = response.json()["result"]["structuredContent"]
    assert found["statement"] == "Сложи $a$ и $b$."
    assert found["open_tests"] == [{"input": "1 2\n", "output": "3\n"}]
    assert found["closed_tests"] == 1


async def test_closing_the_section_cuts_off_student_keys(session, client):
    """Галочка в «Видимости разделов» выключает нейросети студентам, например
    на контрольную, — не нужно отзывать ключи по одному."""
    anya = await _user(session, "Аня")
    teacher = await _user(session, "Кирилл", Role.teacher)
    student_key, teacher_key = await _key(session, anya), await _key(session, teacher)
    await features.save(session, "ai", for_students=False, for_teachers=True)

    closed = await _rpc(client, student_key, "tools/list")
    assert closed.status_code == 403 and "закрыт" in closed.json()["error"]["message"]
    assert (await _rpc(client, teacher_key, "tools/list")).status_code == 200


# ---------------------------------------------------------------- преподаватель


async def test_read_only_teacher_key_cannot_change_the_portal(session, client):
    teacher = await _user(session, "Кирилл", Role.teacher)
    key = await _key(session, teacher)
    names = await _tool_names(client, key)
    assert "search_problems" in names and "create_problem_set" not in names

    refused = await _call(client, key, "create_problem_set", title="x", problems=["two-sum"])
    assert "только на чтение" in refused["error"]
    assert await session.scalar(select(ProblemSet)) is None


async def test_preview_warns_about_problems_the_group_already_had(session, client):
    teacher = await _user(session, "Кирилл", Role.teacher)
    old = await _problem(session, "two-sum")
    await _problem(session, "binary-search")
    await _assignment(session, await _group_with(session, "ПМИ-1"), old)
    key = await _key(session, teacher)

    preview = await _call(
        client, key, "preview_problem_list",
        problems=["https://leetcode.com/problems/two-sum/", "binary-search", "no-such-problem"],
    )
    by_ref = {item["ref"]: item for item in preview["resolved"]}
    assert by_ref["lc:two-sum"]["used_in"][0]["assignments"][0]["for"] == "ПМИ-1"
    assert by_ref["lc:binary-search"]["used_in"] == []
    assert preview["unresolved"] == ["no-such-problem"]
    assert await session.scalar(select(ProblemSet).where(ProblemSet.title == "x")) is None


async def test_teacher_creates_a_set_the_same_way_as_the_form(session, client):
    teacher = await _user(session, "Кирилл", Role.teacher)
    await _problem(session, "two-sum")
    await _problem(session, "binary-search")
    key = await _key(session, teacher, can_write=True)

    created = await _call(
        client, key, "create_problem_set",
        title="Бинпоиск", problems=["binary-search", "two-sum", "nope"],
    )
    assert created["added"] == ["lc:binary-search", "lc:two-sum"]
    assert created["unresolved"] == ["nope"]

    problem_set = await session.get(ProblemSet, created["id"])
    await session.refresh(problem_set)
    assert problem_set.created_by_id == teacher.id
    assert [item.problem.slug for item in problem_set.items] == ["binary-search", "two-sum"]

    added = await _call(client, key, "add_problems_to_set",
                        problem_set_id=problem_set.id, problems=["two-sum"])
    assert added["added"] == []      # уже стоит — второй раз не добавляется


async def test_search_finds_problems_by_whole_tag(session, client):
    teacher = await _user(session, "Кирилл", Role.teacher)
    await _problem(session, "two-sum")
    other = await _problem(session, "climbing-stairs")
    other.tags = ["Dynamic Programming"]
    await session.commit()
    key = await _key(session, teacher)

    found = await _call(client, key, "search_problems", tag="binary search")
    assert [p["ref"] for p in found["problems"]] == ["lc:two-sum"]
    assert (await _call(client, key, "search_problems", tag="binary"))["problems"] == []


async def test_assignment_is_not_issued_to_everyone_by_accident(session, client):
    """В форме пустое «Кому» — это «всем». От модели такого молчаливого
    смысла не принимаем: всем — только если сказано явно."""
    teacher = await _user(session, "Кирилл", Role.teacher)
    group = await _group_with(session, "ПМИ-1")
    problem_set = ProblemSet(title="Список")
    session.add(problem_set)
    await session.commit()
    key = await _key(session, teacher, can_write=True)
    # Отказ откатывает сессию, а она в тестах общая с порталом: объекты
    # после него протухают, поэтому номера берём заранее.
    group_id, set_id = group.id, problem_set.id

    refused = await _call(client, key, "create_assignment", title="ДЗ 1", problem_set_id=set_id)
    assert "кому" in refused["error"].lower()
    assert await session.scalar(select(Assignment)) is None

    deadline = datetime(2030, 1, 10, 23, 59)
    created = await _call(client, key, "create_assignment", title="ДЗ 1",
                          problem_set_id=set_id, group_ids=[group_id],
                          deadline=deadline.isoformat())
    issued = created["assignments"][0]
    assert issued["for"] == "ПМИ-1"
    # Время без пояса — местное время портала, как в форме на сайте.
    assert issued["deadline"].startswith("2030-01-10T23:59")


async def test_assignment_errors_come_back_to_the_model(session, client):
    teacher = await _user(session, "Кирилл", Role.teacher)
    key = await _key(session, teacher, can_write=True)
    refused = await _call(client, key, "create_assignment", title="ДЗ", problem_set_id=999,
                          for_everyone=True)
    assert "не найден" in refused["error"]


async def test_bad_arguments_are_explained_not_crashed(session, client):
    teacher = await _user(session, "Кирилл", Role.teacher)
    key = await _key(session, teacher)
    refused = await _call(client, key, "get_assignment", assignment_id="первое", extra=1)
    assert "Неверные параметры" in refused["error"]


async def test_solutions_for_review_include_students_but_not_the_teacher(session, client):
    """Преподаватель прорешивает контрольную заранее — его код в сравнение не идёт."""
    teacher = await _user(session, "Кирилл", Role.teacher)
    anya = await _user(session, "Аня")
    boris = await _user(session, "Борис")
    problem = await _problem(session, "two-sum")
    group = await _group_with(session, "ПМИ-1", anya, boris, teacher)
    assignment = await _assignment(session, group, problem, requires_solution=True)
    now = utcnow()
    session.add_all([
        SolutionUpload(assignment_id=assignment.id, problem_id=problem.id, user_id=anya.id,
                       code="def solve(): return 'anya'"),
        Submission(user_id=boris.id, platform=Platform.leetcode, external_id="b1",
                   problem_id=problem.id, verdict="Wrong Answer", is_accepted=False,
                   code="print('first try')", submitted_at=now - timedelta(minutes=5)),
        Submission(user_id=boris.id, platform=Platform.leetcode, external_id="b2",
                   problem_id=problem.id, verdict="Accepted", is_accepted=True,
                   code="print('boris')", submitted_at=now - timedelta(minutes=1)),
        Submission(user_id=boris.id, platform=Platform.leetcode, external_id="b3",
                   problem_id=problem.id, verdict="Wrong Answer", is_accepted=False,
                   code="print('after accepted')", submitted_at=now),
        Submission(user_id=teacher.id, platform=Platform.leetcode, external_id="t1",
                   problem_id=problem.id, verdict="Accepted", is_accepted=True,
                   code="print('reference')", submitted_at=now),
    ])
    await session.commit()
    key = await _key(session, teacher)

    found = await _call(client, key, "get_assignment_solutions", assignment_id=assignment.id)
    solutions = {s["student"]: s for s in found["problems"][0]["solutions"]}
    assert set(solutions) == {"Аня", "Борис"}
    assert solutions["Аня"]["source"] == "upload"
    # Принятое решение важнее более позднего неудачного.
    assert solutions["Борис"]["code"] == "print('boris')"
    assert solutions["Борис"]["attempts"] == 3
    assert "reference" not in json.dumps(found, ensure_ascii=False)


async def test_progress_matrix_counts_like_the_site(session, client):
    teacher = await _user(session, "Кирилл", Role.teacher)
    anya = await _user(session, "Аня")
    problem = await _problem(session, "two-sum")
    assignment = await _assignment(session, await _group_with(session, "ПМИ-1", anya), problem,
                                   deadline=utcnow() - timedelta(hours=1))
    session.add(
        Submission(user_id=anya.id, platform=Platform.leetcode, external_id="1",
                   problem_id=problem.id, verdict="Accepted", is_accepted=True,
                   submitted_at=utcnow())
    )
    await session.commit()
    key = await _key(session, teacher)

    matrix = await _call(client, key, "get_assignment_progress", assignment_id=assignment.id)
    row = matrix["students"][0]
    # Решено после дедлайна: видно, но не засчитано — так же, как в матрице.
    assert row["problems"][0]["status"] == "solved_late"
    assert row["solved"] == 0


async def test_demoted_teacher_key_loses_teacher_tools(session, client):
    """Роль берётся у человека при каждом запросе, а не записана в ключ."""
    teacher = await _user(session, "Кирилл", Role.teacher)
    key = await _key(session, teacher, can_write=True)
    teacher.role = Role.student
    await session.commit()
    assert "create_problem_set" not in await _tool_names(client, key)


# ---------------------------------------------------------------- раздел «ИИ»


async def test_new_key_is_shown_once(session, client):
    await client.post("/login/dev", data={"name": "Кирилл", "teacher": "true"})
    issued = await client.post("/ai/tokens", data={"name": "Claude", "can_write": "true"})
    token = await session.scalar(select(ApiToken))
    raw = next(
        word for word in issued.text.replace("<", " ").replace(">", " ").split()
        if word.startswith(api_tokens.PREFIX) and len(word) > 20
    )
    assert token.can_write is True
    assert f"Bearer {raw}" in issued.text          # настройки сразу с ключом
    assert raw not in (await client.get("/ai")).text


async def test_new_badge_hangs_for_a_week_and_then_goes(session, client, monkeypatch):
    await client.post("/login/dev", data={"name": "Аня"})
    monkeypatch.setattr(features, "utcnow", lambda: datetime(2026, 10, 16, 12, tzinfo=UTC))
    page = (await client.get("/")).text
    assert 'href="/ai"' in page and 'class="nav-new"' in page

    monkeypatch.setattr(features, "utcnow", lambda: datetime(2026, 10, 23, 12, tzinfo=UTC))
    page = (await client.get("/")).text
    assert 'href="/ai"' in page and 'class="nav-new"' not in page


async def test_ai_section_can_be_hidden_from_students(session, client):
    await client.post("/login/dev", data={"name": "Кирилл", "teacher": "true"})
    form = {f"{key}:{who}": "on" for key in ("materials", "course", "grades")
            for who in ("students", "teachers")}
    form["ai:teachers"] = "on"
    await client.post("/view/admin")
    await client.post("/teacher/features", data=form)
    await client.post("/logout")

    await client.post("/login/dev", data={"name": "Аня"})
    assert 'href="/ai"' not in (await client.get("/")).text
    assert "Раздел сейчас закрыт" in (await client.get("/ai")).text


async def test_ai_page_lists_what_the_role_can_do(session, client):
    await client.post("/login/dev", data={"name": "Кирилл", "teacher": "true"})
    await client.post("/logout")
    await client.post("/login/dev", data={"name": "Аня"})
    page = (await client.get("/ai")).text
    assert "list_assignments" in page and "get_assignment_solutions" not in page
    assert "/mcp" in page
    # Человеку — своя строка про каждый инструмент и прямо сказанные границы:
    # описание для модели длинное, и из него студент этого не узнает.
    assert "условие, лимиты и открытые тесты" in page
    assert "Чего нейросеть не сможет" in page and "получить закрытые тесты" in page


def test_every_tool_explains_itself_to_people():
    from app.mcp.registry import TOOLS

    for item in TOOLS.values():
        # Строка под названием, а не абзац: длинную никто не дочитает.
        assert item.hint and len(item.hint) <= 60, item.name


async def test_student_form_cannot_smuggle_in_a_write_key(session, client):
    """Галочки у студента нет, но и запрос в обход формы не даёт ключ на запись."""
    await client.post("/login/dev", data={"name": "Кирилл", "teacher": "true"})
    await client.post("/logout")
    await client.post("/login/dev", data={"name": "Аня"})
    page = await client.post("/ai/tokens", data={"name": "хитрый", "can_write": "true"})
    assert 'name="can_write"' not in page.text
    token = await session.scalar(select(ApiToken))
    assert token.can_write is False


async def test_teacher_looking_as_a_student_sees_the_student_page(session, client):
    """Иначе не проверить, что студенту обещано и что ему доступно."""
    await client.post("/login/dev", data={"name": "Кирилл", "teacher": "true"})
    teacher_page = (await client.get("/ai")).text
    assert "get_assignment_solutions" in teacher_page and 'name="can_write"' in teacher_page

    await client.post("/view/student", data={"next": "/"})
    page = (await client.get("/ai")).text
    assert "Так раздел видит студент" in page
    assert "list_assignments" in page
    for teacher_only in ("get_assignment_solutions", "create_problem_set", "create_assignment"):
        assert teacher_only not in page, teacher_only
    # Ключ преподавателя дал бы его инструменты — выпускать его отсюда нельзя.
    assert 'action="/ai/tokens"' not in page
    assert "сдать решение или что-то изменить" in page


# ---------------------------------------------------------------- оценки и закрытые разделы


async def _gradebook(session, group, *students):
    """Занятие с явкой и домашкой, у каждого студента — свои отметки."""
    from app.models import Attendance, SheetColumn, SheetKind, SheetLesson, SheetMark, SheetScale

    lesson = SheetLesson(group_id=group.id, title="Неделя 1", position=0)
    session.add(lesson)
    await session.commit()
    seen = SheetColumn(group_id=group.id, lesson_id=lesson.id, title="явка",
                       kind=SheetKind.attendance, scale=SheetScale.pass_fail, position=0)
    homework = SheetColumn(group_id=group.id, lesson_id=lesson.id, title="домашка",
                           kind=SheetKind.manual, scale=SheetScale.pass_fail, position=1)
    session.add_all([seen, homework])
    await session.commit()
    for number, student in enumerate(students):
        session.add_all([
            SheetMark(column_id=seen.id, user_id=student.id, attendance=Attendance.present),
            SheetMark(column_id=homework.id, user_id=student.id, passed=number == 0,
                      comment=f"комментарий для {student.display_name}"),
        ])
    await session.commit()


async def test_student_sees_own_grades_and_nobody_elses(session, client):
    anya = await _user(session, "Аня")
    boris = await _user(session, "Борис")
    await _gradebook(session, await _group_with(session, "ПМИ-1", anya, boris), anya, boris)
    key = await _key(session, anya)

    response = await _rpc(client, key, "tools/call", {"name": "get_my_grades", "arguments": {}})
    assert "Борис" not in response.text
    grades = response.json()["result"]["structuredContent"]["groups"][0]
    by_title = {work["title"]: work for work in grades["works"]}
    assert by_title["явка"]["attendance"] == "present"
    assert by_title["домашка"]["passed"] is True
    assert grades["total"]["passed"] == 1


async def test_closed_section_hides_its_tools_from_that_role(session, client):
    """Оценки скрыты от студентов — их инструменты тоже: страница и нейросеть
    не показывают разное. Преподавателю раздел открыт, и ведомость при нём."""
    anya = await _user(session, "Аня")
    teacher = await _user(session, "Кирилл", Role.teacher)
    group = await _group_with(session, "ПМИ-1", anya)
    await _gradebook(session, group, anya)
    student_key, teacher_key = await _key(session, anya), await _key(session, teacher)
    group_id = group.id

    assert "get_my_grades" in await _tool_names(client, student_key)
    await features.save(session, "grades", for_students=False, for_teachers=True)

    assert "get_my_grades" not in await _tool_names(client, student_key)
    refused = await _call(client, student_key, "get_my_grades")
    assert "закрыт" in refused["error"]

    book = await _call(client, teacher_key, "get_gradebook", group_id=group_id)
    assert [s["student"] for s in book["students"]] == ["Аня"]
    assert book["students"][0]["marks"][1]["comment"] == "комментарий для Аня"

    await features.save(session, "grades", for_students=False, for_teachers=False)
    assert "get_gradebook" not in await _tool_names(client, teacher_key)


async def test_ai_page_does_not_promise_tools_of_a_closed_section(session, client):
    await client.post("/login/dev", data={"name": "Кирилл", "teacher": "true"})
    await features.save(session, "grades", for_students=False, for_teachers=True)
    teacher_page = (await client.get("/ai")).text
    assert "get_gradebook" in teacher_page

    await client.post("/view/student", data={"next": "/"})
    student_page = (await client.get("/ai")).text
    assert "get_my_grades" not in student_page and "list_assignments" in student_page


async def test_what_people_copy_has_a_copy_button(session, client):
    """Адрес, ключ и настройки клиентов переносят в другую программу —
    кнопка рядом, а не выделение текста наугад."""
    await client.post("/login/dev", data={"name": "Кирилл", "teacher": "true"})
    page = (await client.get("/ai")).text
    assert page.count("data-copy") >= 2          # адрес и настройки клиентов
    assert "/static/copy.js" in page


# ---------------------------------------------------------------- сроки, проверка, студенты


async def _late_solver(session, group_title, problem, *, deadline):
    """Задание группы, которое студент решил после срока."""
    student = await _user(session, f"Студент {group_title}")
    group = await _group_with(session, group_title, student)
    assignment = await _assignment(session, group, problem, deadline=deadline)
    session.add(
        Submission(user_id=student.id, platform=Platform.leetcode,
                   external_id=f"late-{group_title}", problem_id=problem.id,
                   verdict="Accepted", is_accepted=True,
                   submitted_at=deadline + timedelta(hours=6))
    )
    await session.commit()
    return assignment


async def test_deadline_preview_shows_gained_credits_and_writes_nothing(session, client):
    teacher = await _user(session, "Кирилл", Role.teacher)
    problem = await _problem(session, "two-sum")
    old = utcnow() - timedelta(hours=12)
    first = await _late_solver(session, "ПМИ-1", problem, deadline=old)
    second = await _late_solver(session, "ПМИ-2", problem, deadline=old)
    ids = [first.id, second.id]
    key = await _key(session, teacher, can_write=True)
    new = (utcnow() + timedelta(days=2)).isoformat()

    preview = await _call(client, key, "extend_deadline",
                          assignment_ids=ids, deadline=new, dry_run=True)
    assert [(a["credits_before"], a["credits_after"]) for a in preview["assignments"]] == [
        (0, 1), (0, 1)
    ]
    for assignment_id in ids:
        stored = await session.get(Assignment, assignment_id)
        await session.refresh(stored)
        assert stored.deadline < utcnow()

    await _call(client, key, "extend_deadline", assignment_ids=ids, deadline=new)
    for assignment_id in ids:
        stored = await session.get(Assignment, assignment_id)
        await session.refresh(stored)
        assert stored.deadline > utcnow()


async def test_deadline_moves_for_all_assignments_or_for_none(session, client):
    """Половина продлённых заданий хуже ни одного: не вспомнить, какие сдвинуты."""
    teacher = await _user(session, "Кирилл", Role.teacher)
    problem = await _problem(session, "two-sum")
    soon = utcnow() + timedelta(days=1)
    later = await _late_solver(session, "ПМИ-1", problem, deadline=soon + timedelta(days=5))
    earlier = await _late_solver(session, "ПМИ-2", problem, deadline=soon - timedelta(days=1))
    ids = [later.id, earlier.id]
    key = await _key(session, teacher, can_write=True)

    refused = await _call(client, key, "extend_deadline",
                          assignment_ids=ids, deadline=soon.isoformat())
    assert "Ничего не изменено" in refused["error"]
    assert f"задание {ids[0]}" in refused["error"]     # раньше прежнего — назад нельзя
    # Второе задание само по себе продлевалось бы — но не продлено.
    untouched = await session.get(Assignment, ids[1])
    await session.refresh(untouched)
    assert untouched.deadline < soon


async def test_deadline_is_moved_or_removed_only_when_said_explicitly(session, client):
    teacher = await _user(session, "Кирилл", Role.teacher)
    key = await _key(session, teacher, can_write=True)
    refused = await _call(client, key, "extend_deadline", assignment_ids=[1])
    assert "remove_deadline" in refused["error"]


async def test_edit_assignment_keeps_what_was_not_passed(session, client):
    teacher = await _user(session, "Кирилл", Role.teacher)
    problem = await _problem(session, "two-sum")
    assignment = await _assignment(session, await _group_with(session, "ПМИ-1"), problem,
                                   description="Бинпоиск")
    assignment_id = assignment.id
    key = await _key(session, teacher, can_write=True)

    edited = await _call(client, key, "edit_assignment",
                         assignment_id=assignment_id, title="ДЗ 3")
    assert (edited["title"], edited["description"]) == ("ДЗ 3", "Бинпоиск")
    refused = await _call(client, key, "edit_assignment", assignment_id=assignment_id, title=" ")
    assert "Пустое название" in refused["error"]


async def test_rejected_code_needs_a_reason_and_removes_the_credit(session, client):
    teacher = await _user(session, "Кирилл", Role.teacher)
    anya = await _user(session, "Аня")
    problem = await _problem(session, "two-sum")
    assignment = await _assignment(session, await _group_with(session, "ПМИ-1", anya), problem,
                                   requires_solution=True)
    session.add_all([
        SolutionUpload(assignment_id=assignment.id, problem_id=problem.id, user_id=anya.id,
                       code="print('copied')"),
        Submission(user_id=anya.id, platform=Platform.leetcode, external_id="a1",
                   problem_id=problem.id, verdict="Accepted", is_accepted=True,
                   submitted_at=utcnow()),
    ])
    await session.commit()
    assignment_id, anya_id = assignment.id, anya.id
    key = await _key(session, teacher, can_write=True)

    waiting = await _call(client, key, "list_pending_reviews")
    assert [(p["student_id"], p["code"]) for p in waiting["pending"]] == [
        (anya_id, "print('copied')")
    ]

    refused = await _call(client, key, "review_solution", assignment_id=assignment_id,
                          problem="two-sum", student_id=anya_id, decision="reject")
    assert "без объяснения" in refused["error"]

    reviewed = await _call(client, key, "review_solution", assignment_id=assignment_id,
                           problem="two-sum", student_id=anya_id, decision="reject",
                           comment="Совпадает с решением соседа")
    assert reviewed["status"] == "rejected"
    matrix = await _call(client, key, "get_assignment_progress", assignment_id=assignment_id)
    assert matrix["students"][0]["solved"] == 0
    assert (await _call(client, key, "list_pending_reviews"))["pending"] == []


async def test_announcement_is_not_sent_to_everyone_by_accident(session, client):
    from app.models import Announcement

    teacher = await _user(session, "Кирилл", Role.teacher)
    group = await _group_with(session, "ПМИ-1")
    group_id = group.id
    key = await _key(session, teacher, can_write=True)

    refused = await _call(client, key, "create_announcement", title="Контест в субботу")
    assert "кому" in refused["error"].lower()
    assert await session.scalar(select(Announcement)) is None

    created = await _call(client, key, "create_announcement", title="Контест в субботу",
                          group_id=group_id, url="https://codeforces.com/contests")
    assert created["for"] == "ПМИ-1"


async def test_student_is_found_by_name_in_any_case(session, client):
    """SQLite сравнивает без регистра только латиницу — а фамилии кириллицей."""
    teacher = await _user(session, "Кирилл", Role.teacher)
    petrov = await _user(session, "vasya")
    petrov.official_name = "Петров Василий"
    await session.commit()
    await _group_with(session, "ПМИ-1", petrov)
    key = await _key(session, teacher)

    found = await _call(client, key, "find_students", query="петров")
    assert [(s["name"], s["groups"]) for s in found["students"]] == [("vasya", ["ПМИ-1"])]


async def test_student_overview_counts_like_the_matrix(session, client):
    teacher = await _user(session, "Кирилл", Role.teacher)
    problem = await _problem(session, "two-sum")
    assignment = await _late_solver(session, "ПМИ-1", problem,
                                    deadline=utcnow() - timedelta(hours=12))
    student = await session.scalar(select(User).where(User.display_name == "Студент ПМИ-1"))
    student_id, assignment_id = student.id, assignment.id
    key = await _key(session, teacher)

    overview = await _call(client, key, "get_student", student_id=student_id)
    assert overview["groups"] == [{"id": assignment.group_id, "title": "ПМИ-1"}]
    # Решено после срока: в матрице не засчитано — и здесь тоже.
    row = next(a for a in overview["assignments"] if a["id"] == assignment_id)
    assert (row["solved"], row["total"]) == (0, 1)
