"""Раздел «Курс»: что видно в оглавлении, что открывается, что не отдаётся.

Материалы курса лежат файлами в `course/`, а не в базе, поэтому тесты смотрят
на настоящую выгрузку — как её увидит студент.
"""

from __future__ import annotations

import pytest

from app.services import course


async def _login(client, name="Аня", teacher=False):
    data = {"name": name}
    if teacher:
        data["teacher"] = "true"
    await client.post("/login/dev", data=data)


pytestmark = pytest.mark.skipif(
    not course.is_available(), reason="курс не выгружен: python scripts/sync_course.py <клон>"
)


async def test_index_lists_weeks(session, client):
    await _login(client)
    page = await client.get("/course")
    assert page.status_code == 200
    assert "Неделя 1" in page.text
    assert "Неделя 16" in page.text


async def test_week_shows_card_and_slots(session, client):
    await _login(client)
    page = await client.get("/course/1/02")
    assert page.status_code == 200
    # Текст карточки — из card.md, слоты — по наличию ноутбуков.
    assert "Лекция опережает практику" in page.text
    assert "конспект" in page.text
    assert "практика" in page.text


async def test_notebook_opens_on_the_portal(session, client):
    await _login(client)
    page = await client.get("/course/1/01/lecture")
    assert page.status_code == 200
    assert "notebook" in page.text


async def test_week_without_notebooks_is_marked_ahead(session, client):
    await _login(client)
    page = await client.get("/course")
    # Ноутбуков за неделю 16 ещё нет — карточка должна честно это показывать.
    assert "впереди" in page.text


async def test_missing_week_goes_back_with_error(session, client):
    await _login(client)
    response = await client.get("/course/1/99", follow_redirects=False)
    assert response.status_code == 303
    assert "/course?err=" in response.headers["location"]


async def test_file_name_cannot_escape_the_week(session, client):
    """Имя файла приходит из адреса, поэтому каталоги из него выкидываются."""
    await _login(client)
    response = await client.get("/course/1/02/file/..%2F..%2Fpages%2Fabout.md")
    assert response.status_code in {303, 404}


async def test_attachment_is_downloaded_not_opened(session, client):
    await _login(client)
    response = await client.get("/course/1/02/file/lab02_tests.py")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]


async def test_about_page_renders(session, client):
    await _login(client)
    page = await client.get("/course/about")
    assert page.status_code == 200
    assert "Курс" in page.text


async def test_anonymous_is_not_let_in(session, client):
    response = await client.get("/course", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")
