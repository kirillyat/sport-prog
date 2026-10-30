"""Режим администратора: настройки портала убраны с глаз преподавателя.

Каждый день преподавателю нужны группы, задания и проверка. Судья, ключи
модели, видимость разделов и выдача ролей нужны редко, а случайная правка
там бьёт по всем группам сразу. Поэтому по умолчанию их нет ни в рейке,
ни по прямому адресу; режим включает себе любой преподаватель.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import FeatureFlag, Role, User

ADMIN_PAGES = (
    "/teacher/integrations",
    "/teacher/features",
    "/teacher/problems/unlinked",
)
# Действия — POST. Пустая форма: без режима до разбора формы дойти не должно.
ADMIN_ACTIONS = (
    "/teacher/judge",
    "/teacher/judge/check",
    "/teacher/judge/off",
    "/teacher/judge/languages",
    "/teacher/judge/languages/refresh",
    "/teacher/llm",
    "/teacher/llm/check",
    "/teacher/llm/off",
    "/teacher/features",
    "/teacher/catalog-refresh",
    "/teacher/course-refresh",
)


async def _login(client, name, teacher=False):
    data = {"name": name}
    if teacher:
        data["teacher"] = "true"
    await client.post("/login/dev", data=data)


async def _student(session) -> User:
    student = User(display_name="Аня")
    session.add(student)
    await session.commit()
    return student


@pytest.mark.parametrize("path", ADMIN_PAGES)
async def test_teacher_without_admin_mode_is_refused_a_settings_page(session, client, path):
    await _login(client, "Кирилл", teacher=True)
    page = await client.get(path)
    assert page.status_code == 403
    # Отказ сразу предлагает включить режим и вернуться на ту же страницу.
    assert 'action="/view/admin"' in page.text
    assert f'name="next" value="{path}"' in page.text


@pytest.mark.parametrize("path", ADMIN_ACTIONS)
async def test_teacher_without_admin_mode_cannot_change_settings(session, client, path):
    await _login(client, "Кирилл", teacher=True)
    assert (await client.post(path, data={})).status_code == 403


async def test_closing_a_section_without_admin_mode_changes_nothing(session, client):
    await _login(client, "Кирилл", teacher=True)
    await client.post("/teacher/features", data={})
    assert await session.scalar(select(FeatureFlag)) is None


async def test_role_is_not_granted_without_admin_mode(session, client):
    await _login(client, "Кирилл", teacher=True)
    student = await _student(session)
    page = await client.post(f"/teacher/students/{student.id}/role")
    assert page.status_code == 403
    await session.refresh(student)
    assert student.role == Role.student


async def test_admin_mode_opens_every_settings_page(session, client):
    await _login(client, "Кирилл", teacher=True)
    await client.post("/view/admin")
    for path in ADMIN_PAGES:
        assert (await client.get(path)).status_code == 200, path


async def test_admin_mode_grants_the_role(session, client):
    await _login(client, "Кирилл", teacher=True)
    student = await _student(session)
    await client.post("/view/admin")
    await client.post(f"/teacher/students/{student.id}/role")
    await session.refresh(student)
    assert student.role == Role.teacher


async def test_settings_are_out_of_sight_by_default(session, client):
    await _login(client, "Кирилл", teacher=True)
    rail = (await client.get("/teacher")).text
    for href in ("/teacher/integrations", "/teacher/features", "/teacher/problems/unlinked"):
        assert f'href="{href}"' not in rail, href
    assert 'action="/teacher/catalog-refresh"' not in rail
    # Ежедневная работа на месте.
    for href in ("/teacher/groups", "/teacher/assignments", "/teacher/reviews"):
        assert f'href="{href}"' in rail, href


async def test_admin_mode_shows_the_settings(session, client):
    await _login(client, "Кирилл", teacher=True)
    await client.post("/view/admin")
    page = (await client.get("/teacher")).text
    for href in ("/teacher/integrations", "/teacher/features", "/teacher/problems/unlinked"):
        assert f'href="{href}"' in page, href
    assert 'action="/teacher/catalog-refresh"' in page


async def test_role_button_appears_only_in_admin_mode(session, client):
    await _login(client, "Кирилл", teacher=True)
    student = await _student(session)
    button = f'action="/teacher/students/{student.id}/role"'
    assert button not in (await client.get("/teacher/students")).text
    await client.post("/view/admin")
    assert button in (await client.get("/teacher/students")).text


async def test_switching_admin_mode_off_closes_the_settings_again(session, client):
    await _login(client, "Кирилл", teacher=True)
    await client.post("/view/admin")
    back = await client.post("/view/admin/off", follow_redirects=False)
    assert back.status_code == 303
    assert (await client.get("/teacher/integrations")).status_code == 403


async def test_student_cannot_switch_admin_mode_on(session, client):
    await _login(client, "Кирилл", teacher=True)   # иначе Аня станет первой и получит роль
    await client.post("/logout")
    await _login(client, "Аня")
    switched = await client.post("/view/admin", follow_redirects=False)
    assert "admin_mode" not in switched.headers.get("set-cookie", "")
    # И поддельная кука не помогает: сначала нужна роль преподавателя.
    client.cookies.set("admin_mode", "on")
    assert "только для преподавателя" in (await client.get("/teacher/integrations")).text


async def test_admin_mode_leaves_the_student_view(session, client):
    """Глазами студента настроек не видно: включённый режим без выхода
    из этого взгляда ничего бы не показал."""
    await _login(client, "Кирилл", teacher=True)
    await client.post("/view/student")
    await client.post("/view/admin")
    assert 'href="/teacher/integrations"' in (await client.get("/teacher")).text
