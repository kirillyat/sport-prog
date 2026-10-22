"""Похожие решения: что считается похожим, что нет, и кому это видно."""

from __future__ import annotations

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
    SolutionUpload,
    Submission,
    User,
    utcnow,
)
from app.services import api_tokens
from app.services.similarity import compare, fingerprint, similarity

ORIGINAL = """
n, target = map(int, input().split())
nums = list(map(int, input().split()))
seen_positions = {}
for i, x in enumerate(nums):
    if target - x in seen_positions:
        print(seen_positions[target - x], i)
        break
    seen_positions[x] = i
"""

# Та же программа: другие имена, комментарии, пустые строки.
DISGUISED = """
# моё решение, сам писал
k, goal = map(int, input().split())

arr = list(map(int, input().split()))
memo = {}
for idx, value in enumerate(arr):
    if goal - value in memo:   # нашли пару
        print(memo[goal - value], idx)
        break
    memo[value] = idx
"""

# Та же задача, но своё решение: сортировка и два указателя.
OWN_WAY = """
n, t = map(int, input().split())
a = list(map(int, input().split()))
order = sorted(range(n), key=lambda i: a[i])
lo, hi = 0, n - 1
while lo < hi:
    s = a[order[lo]] + a[order[hi]]
    if s == t:
        print(min(order[lo], order[hi]), max(order[lo], order[hi]))
        break
    if s < t:
        lo += 1
    else:
        hi -= 1
"""


def test_renamed_variables_and_comments_do_not_hide_a_copy():
    assert similarity(fingerprint(ORIGINAL), fingerprint(DISGUISED)) >= 0.9


def test_an_own_solution_of_the_same_problem_is_not_reported():
    result = compare({1: fingerprint(ORIGINAL), 2: fingerprint(OWN_WAY)})
    assert result.pairs == []
    assert result.compared == 2


def test_short_solutions_are_not_compared():
    """У задачи в две строки хорошие решения совпадают сами по себе."""
    short = "print(sum(map(int, input().split())))"
    result = compare({1: fingerprint(short), 2: fingerprint(short)})
    assert result.pairs == [] and result.short == 2


def test_floor_division_in_python_is_code_but_in_cpp_a_comment():
    # x = a / / b — шесть элементов: «//» у Python — деление, а не комментарий.
    assert fingerprint("x = a // b").tokens == 6
    # int x = 1 ; — комментарий после «//» у C++ выброшен.
    assert fingerprint("int x = 1; // пояснение длинное", "GNU G++17").tokens == 5


def test_a_rare_name_shared_only_by_the_pair_is_evidence():
    """Имя, которое есть у всей группы (nums из условия), уликой не бывает."""
    copy_with_helper = ORIGINAL.replace("seen_positions", "zz_positions_map")
    result = compare({
        1: fingerprint(copy_with_helper),
        2: fingerprint(copy_with_helper.replace("target", "goal")),
        3: fingerprint(OWN_WAY.replace("a = ", "nums = ").replace("a[", "nums[")),
    })
    [pair] = result.pairs
    assert (pair.first, pair.second) == (1, 2)
    assert "zz_positions_map" in pair.shared_names
    assert "nums" not in pair.shared_names


def test_names_from_the_statement_are_not_evidence():
    """nums и target даёт сигнатура LeetCode: если остальные их переименовали,
    они «есть только у двоих», но списывания это не доказывает."""
    result = compare({
        1: fingerprint(ORIGINAL),
        2: fingerprint(ORIGINAL.replace("i, x", "j, y").replace("[x]", "[y]")),
        3: fingerprint(DISGUISED),
    })
    pair = next(p for p in result.pairs if (p.first, p.second) == (1, 2))
    assert "seen_positions" in pair.shared_names
    assert not {"nums", "target"} & set(pair.shared_names)


# ---------------------------------------------------------------- страница и MCP


async def _setup(session):
    teacher = User(display_name="Кирилл", role=Role.teacher)
    anya, boris, vera = (User(display_name=name) for name in ("Аня", "Борис", "Вера"))
    problem = Problem(platform=Platform.leetcode, external_id="1", slug="two-sum",
                      title="Two Sum", url="https://leetcode.com/problems/two-sum/")
    session.add_all([teacher, anya, boris, vera, problem])
    await session.commit()
    group = Group(title="ПМИ-1", join_code="PMI1")
    problem_set = ProblemSet(title="Контрольная")
    session.add_all([group, problem_set])
    await session.commit()
    session.add_all([
        *(GroupMembership(group_id=group.id, user_id=u.id) for u in (teacher, anya, boris, vera)),
        ProblemSetItem(problem_set_id=problem_set.id, problem_id=problem.id),
    ])
    assignment = Assignment(title="Контрольная 1", problem_set_id=problem_set.id,
                            group_id=group.id, assigned_at=utcnow() - timedelta(days=1),
                            requires_solution=True)
    session.add(assignment)
    await session.commit()
    now = utcnow()
    session.add_all([
        SolutionUpload(assignment_id=assignment.id, problem_id=problem.id, user_id=anya.id,
                       code=ORIGINAL),
        SolutionUpload(assignment_id=assignment.id, problem_id=problem.id, user_id=boris.id,
                       code=DISGUISED),
        SolutionUpload(assignment_id=assignment.id, problem_id=problem.id, user_id=vera.id,
                       code=OWN_WAY),
        # Преподаватель прорешал заранее тем же кодом — его в сравнение не берём.
        Submission(user_id=teacher.id, platform=Platform.leetcode, external_id="t1",
                   problem_id=problem.id, verdict="Accepted", is_accepted=True,
                   code=ORIGINAL, submitted_at=now),
    ])
    await session.commit()
    return assignment, problem, teacher, anya, boris


async def test_teacher_sees_the_pair_and_compares_it_side_by_side(session, client):
    assignment, problem, _, anya, boris = await _setup(session)
    await client.post("/login/dev", data={"name": "Кирилл"})

    matrix = (await client.get(f"/teacher/assignments/{assignment.id}")).text
    assert f"/teacher/assignments/{assignment.id}/similar" in matrix

    page = (await client.get(f"/teacher/assignments/{assignment.id}/similar")).text
    assert "Аня" in page and "Борис" in page
    assert "Вера" not in page.split("Two Sum", 1)[1]
    assert "сравнили решений: 3" in page

    pair = await client.get(
        f"/teacher/assignments/{assignment.id}/similar/{problem.id}?a={anya.id}&b={boris.id}"
    )
    assert pair.status_code == 200
    assert "seen_positions" in pair.text and "memo" in pair.text


async def test_a_student_cannot_open_the_report(session, client):
    assignment, problem, _, anya, boris = await _setup(session)
    await client.post("/login/dev", data={"name": "Аня"})
    page = await client.get(f"/teacher/assignments/{assignment.id}/similar")
    assert "Похожие решения" not in page.text
    pair = await client.get(
        f"/teacher/assignments/{assignment.id}/similar/{problem.id}?a={anya.id}&b={boris.id}"
    )
    assert "memo" not in pair.text


async def test_the_ai_gets_the_same_pairs_as_the_page(session, client):
    assignment, _, teacher, anya, _ = await _setup(session)
    _, key = await api_tokens.issue(session, teacher, "тест", can_write=False)
    response = await client.post("/mcp", headers={"Authorization": f"Bearer {key}"}, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "find_similar_solutions", "arguments": {"assignment_id": assignment.id}},
    })
    found = response.json()["result"]["structuredContent"]
    [pair] = found["problems"][0]["pairs"]
    assert {s["name"] for s in pair["students"]} == {"Аня", "Борис"}
    assert pair["similarity"] >= 90
    assert "/similar/" in pair["compare_url"]

    _, student_key = await api_tokens.issue(session, anya, "тест", can_write=False)
    refused = await client.post("/mcp", headers={"Authorization": f"Bearer {student_key}"}, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "find_similar_solutions", "arguments": {"assignment_id": assignment.id}},
    })
    assert refused.json()["result"]["isError"]
