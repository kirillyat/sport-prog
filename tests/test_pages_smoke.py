"""Обход страниц: каждая открывается, и каждый вариант фильтра тоже.

Написан после того, как выбор «Все» в фильтре группы ронял табло и ленту:
маршрут ждал число, а <select> присылал пустую строку. Такое ловится только
проходом по тем значениям, которые страница реально предлагает.
"""

from __future__ import annotations

from html.parser import HTMLParser

import pytest
from sqlalchemy import select

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
)

PAGES = [
    "/", "/materials", "/course", "/announcements", "/feed", "/leaderboard", "/accounts", "/me",
    "/teacher", "/teacher/groups", "/teacher/sets", "/teacher/assignments",
    "/teacher/students", "/teacher/announcements", "/teacher/problems", "/teacher/reviews",
    "/teacher/problems/unlinked",
]


class GetForms(HTMLParser):
    """Собирает поля GET-форм: (action, имя поля, варианты значений)."""

    def __init__(self) -> None:
        super().__init__()
        self.forms: list[tuple[str, dict[str, list[str]]]] = []
        self._action: str | None = None
        self._field: str | None = None

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if tag == "form":
            method = (attr.get("method") or "get").lower()
            self._action = attr.get("action") if method == "get" else None
            if self._action:
                self.forms.append((self._action, {}))
        elif tag == "select" and self._action and attr.get("name"):
            self._field = attr["name"]
            self.forms[-1][1].setdefault(self._field, [])
        elif tag == "option" and self._field:
            self.forms[-1][1][self._field].append(attr.get("value", ""))

    def handle_endtag(self, tag):
        if tag == "select":
            self._field = None
        elif tag == "form":
            self._action = None


@pytest.fixture
async def world(session, client):
    """Минимальный, но непустой портал: группа, студент, список задач, задание."""
    await client.post("/login/dev", data={"name": "Кирилл", "teacher": "true"})
    teacher = await session.scalar(select(User).where(User.display_name == "Кирилл"))
    teacher.role = Role.teacher

    student = User(display_name="Аня")
    group = Group(title="Осень", join_code="SMK123")
    problem = Problem(platform=Platform.leetcode, external_id="1", slug="two-sum",
                      title="Two Sum", url="https://leetcode.com/problems/two-sum/",
                      difficulty="Easy")
    problem_set = ProblemSet(title="Список")
    session.add_all([student, group, problem, problem_set])
    await session.commit()

    session.add_all([
        GroupMembership(group_id=group.id, user_id=student.id),
        ProblemSetItem(problem_set_id=problem_set.id, problem_id=problem.id, position=0),
        Assignment(title="Неделя 1", problem_set_id=problem_set.id, group_id=group.id),
    ])
    await session.commit()
    return {"group": group, "set": problem_set, "student": student}


@pytest.mark.parametrize("path", PAGES)
async def test_page_opens(client, world, path):
    response = await client.get(path)
    assert response.status_code == 200, f"{path} → {response.status_code}"


async def test_every_filter_option_works(client, world):
    """Каждый вариант каждого фильтра на каждой странице должен открываться."""
    checked = 0
    for path in PAGES:
        parser = GetForms()
        parser.feed((await client.get(path)).text)
        for action, fields in parser.forms:
            for name, values in fields.items():
                for value in values:
                    url = f"{action}?{name}={value}"
                    response = await client.get(url)
                    assert response.status_code == 200, f"{url} → {response.status_code}"
                    checked += 1
    # Страховка от молчаливого «ничего не нашли и всё зелено».
    assert checked >= 4
