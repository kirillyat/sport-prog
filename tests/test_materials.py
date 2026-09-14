"""Раздел «Материалы»: кто публикует, кто видит, что принимаем."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.models import Group, GroupMembership, Material, Role, User
from app.services import materials

NOTEBOOK = b'{"cells": [], "nbformat": 4}'


@pytest.fixture(autouse=True)
def storage(tmp_path, monkeypatch):
    """Файлы уезжают во временную папку, а не в data/ рядом с базой."""
    monkeypatch.setattr(materials.settings, "data_dir", tmp_path)
    return tmp_path / "materials"


async def _login(client, name: str, teacher: bool = False):
    data = {"name": name}
    if teacher:
        data["teacher"] = "true"
    await client.post("/login/dev", data=data)


def _upload(name: str = "seminar.ipynb", content: bytes = NOTEBOOK):
    return {"file": (name, content, "application/octet-stream")}


async def test_teacher_publishes_and_student_downloads(session, client, storage):
    await _login(client, "Кирилл", teacher=True)
    response = await client.post(
        "/materials", files=_upload(), data={"title": "Семинар 3", "description": "Сортировки"}
    )
    assert "Материал опубликован" in response.text

    item = await session.scalar(select(Material))
    assert (item.title, item.filename, item.size) == ("Семинар 3", "seminar.ipynb", len(NOTEBOOK))
    assert (storage / item.stored_name).read_bytes() == NOTEBOOK
    assert item.stored_name != item.filename          # на диске — случайное имя

    await client.post("/logout")
    await _login(client, "Аня")
    page = await client.get("/materials")
    assert "Семинар 3" in page.text and "Сортировки" in page.text

    download = await client.get(f"/materials/{item.id}/download")
    assert download.status_code == 200
    assert download.content == NOTEBOOK
    # Именно скачивание: открывать чужой файл на своём домене незачем.
    assert download.headers["content-disposition"].startswith("attachment")
    assert "seminar.ipynb" in download.headers["content-disposition"]


async def test_material_for_a_group_is_hidden_from_others(session, client):
    await _login(client, "Кирилл", teacher=True)
    group = Group(title="Осень", join_code="THEO01")
    outsider = User(display_name="Чужой", role=Role.student)
    session.add_all([group, outsider])
    await session.commit()

    await client.post("/materials", files=_upload(), data={"group_id": str(group.id)})
    item = await session.scalar(select(Material))

    await client.post("/logout")
    await _login(client, "Чужой")
    assert "seminar.ipynb" not in (await client.get("/materials")).text
    denied = await client.get(f"/materials/{item.id}/download")
    assert denied.status_code == 200            # редирект с сообщением, не файл
    assert "не найден" in denied.text

    # Свой в группе — видит и скачивает.
    member = await session.scalar(select(User).where(User.display_name == "Чужой"))
    session.add(GroupMembership(group_id=group.id, user_id=member.id))
    await session.commit()
    assert (await client.get(f"/materials/{item.id}/download")).content == NOTEBOOK


async def test_student_cannot_publish(session, client):
    await _login(client, "Кирилл", teacher=True)   # первый становится преподавателем
    await client.post("/logout")
    await _login(client, "Аня")

    response = await client.post("/materials", files=_upload())
    assert response.status_code == 403
    assert await session.scalar(select(func.count()).select_from(Material)) == 0


@pytest.mark.parametrize(
    ("name", "content", "expected"),
    [
        ("hack.html", b"<script>", "не принимаем"),
        ("solution.exe", b"MZ", "не принимаем"),
        ("empty.ipynb", b"", "пустой"),
    ],
)
async def test_bad_uploads_are_refused(session, client, name, content, expected):
    await _login(client, "Кирилл", teacher=True)
    response = await client.post("/materials", files=_upload(name, content))
    assert expected in response.text
    assert await session.scalar(select(func.count()).select_from(Material)) == 0


async def test_oversized_file_is_refused(session, client, monkeypatch):
    monkeypatch.setattr(materials, "MAX_BYTES", 10)
    await _login(client, "Кирилл", teacher=True)
    response = await client.post("/materials", files=_upload(content=b"x" * 11))
    assert "больше" in response.text
    assert await session.scalar(select(func.count()).select_from(Material)) == 0


async def test_directories_in_the_name_never_reach_the_disk(session, client, storage):
    await _login(client, "Кирилл", teacher=True)
    await client.post("/materials", files=_upload("../../../etc/passwd.ipynb"))

    item = await session.scalar(select(Material))
    assert item.filename == "passwd.ipynb"
    assert (storage / item.stored_name).is_file()


async def test_delete_removes_the_file_too(session, client, storage):
    await _login(client, "Кирилл", teacher=True)
    await client.post("/materials", files=_upload())
    item = await session.scalar(select(Material))
    path = storage / item.stored_name

    response = await client.post(f"/materials/{item.id}/delete")
    assert "Материал удалён" in response.text
    assert await session.scalar(select(func.count()).select_from(Material)) == 0
    assert not path.exists()
