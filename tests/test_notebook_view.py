"""Показ материала на портале: ноутбук, markdown, простой текст."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.models import Material
from app.services import materials

ESC = ""

NB_WITH_EVERYTHING = json.dumps({
    "nbformat": 4,
    "metadata": {"language_info": {"name": "python"}},
    "cells": [
        {"cell_type": "markdown", "source": ["## Сортировки\n", "Инвариант **вставок**.\n"]},
        {"cell_type": "markdown", "source": "<script>alert(1)</script>"},
        {"cell_type": "code", "source": "print('привет')\n", "outputs": [
            {"output_type": "stream", "text": ["привет\n"]},
            {"output_type": "display_data", "data": {
                "image/png": "iVBORw0KGgo=", "text/plain": ["<Figure>"]}},
            {"output_type": "display_data", "data": {"text/html": "<script>alert(2)</script>"}},
            {"output_type": "error", "ename": "ValueError", "evalue": "нет",
             "traceback": [ESC + "[0;31mValueError" + ESC + "[0m: нет"]},
        ]},
    ],
}).encode()

PLAIN_NOTEBOOK = b'{"cells": [], "nbformat": 4}'


@pytest.fixture(autouse=True)
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(materials.settings, "data_dir", tmp_path)
    return tmp_path / "materials"


async def _teacher(client):
    await client.post("/login/dev", data={"name": "Кирилл", "teacher": "true"})


def _upload(name: str, content: bytes):
    return {"file": (name, content, "application/octet-stream")}


async def test_notebook_shows_markdown_code_and_images(session, client):
    await _teacher(client)
    await client.post("/materials", files=_upload("seminar.ipynb", NB_WITH_EVERYTHING))
    item = await session.scalar(select(Material))

    page = (await client.get(f"/materials/{item.id}/view")).text
    assert "<h2>Сортировки</h2>" in page                      # markdown отрисован
    assert "<strong>вставок</strong>" in page
    # Код подсвечен Pygments: имя и строка — в своих тегах.
    assert '<span class="nb">print</span>' in page
    assert '<span class="s1">\'привет\'</span>' in page
    assert 'src="data:image/png;base64,iVBORw0KGgo="' in page
    assert "ValueError: нет" in page and ESC not in page      # без служебных кодов цвета


async def test_notebook_never_brings_its_own_scripts(session, client):
    """Ни markdown-ячейка, ни вывод не должны выполниться на нашем домене."""
    await _teacher(client)
    await client.post("/materials", files=_upload("seminar.ipynb", NB_WITH_EVERYTHING))
    item = await session.scalar(select(Material))

    page = (await client.get(f"/materials/{item.id}/view")).text
    assert "<script>alert(1)</script>" not in page
    assert "<script>alert(2)</script>" not in page
    assert "вывод в формате text/html" in page


async def test_view_button_only_for_files_we_can_show(session, client):
    await _teacher(client)
    await client.post("/materials", files=_upload("seminar.ipynb", PLAIN_NOTEBOOK))
    await client.post("/materials", files=_upload("archive.zip", b"PK-archive"))

    assert (await client.get("/materials")).text.count("Смотреть") == 1

    archive = await session.scalar(select(Material).where(Material.filename == "archive.zip"))
    refused = await client.get(f"/materials/{archive.id}/view")
    assert "можно только скачать" in refused.text


async def test_broken_notebook_falls_back_to_plain_text(session, client):
    await _teacher(client)
    await client.post("/materials", files=_upload("broken.ipynb", b"{not json"))
    item = await session.scalar(select(Material))

    assert "{not json" in (await client.get(f"/materials/{item.id}/view")).text


async def test_markdown_file_is_rendered(session, client):
    await _teacher(client)
    await client.post("/materials", files=_upload("notes.md", "# Лекция 3\n\nТекст.".encode()))
    item = await session.scalar(select(Material))

    assert "<h1>Лекция 3</h1>" in (await client.get(f"/materials/{item.id}/view")).text


async def test_stranger_cannot_view_a_group_material(session, client):
    from app.models import Group, Role, User

    await _teacher(client)
    group = Group(title="Осень", join_code="VIEW01")
    session.add_all([group, User(display_name="Чужой", role=Role.student)])
    await session.commit()
    await client.post("/materials", files=_upload("seminar.ipynb", PLAIN_NOTEBOOK),
                      data={"group_id": str(group.id)})
    item = await session.scalar(select(Material))

    await client.post("/logout")
    await client.post("/login/dev", data={"name": "Чужой"})
    assert "не найден" in (await client.get(f"/materials/{item.id}/view")).text


NB_MATH_AND_SVG = json.dumps({
    "nbformat": 4,
    "metadata": {"language_info": {"name": "python"}},
    "cells": [
        {"cell_type": "markdown",
         "source": "Сложность $O(n \\log n)$.\n\n$$\n\\sum_{i=1}^{n} a_i\n$$\n"},
        {"cell_type": "markdown", "source": "$\\text{<img src=x onerror=alert(3)>}$"},
        {"cell_type": "code", "source": "plot()\n", "outputs": [
            {"output_type": "display_data",
             "data": {"image/svg+xml":
                      "<svg xmlns='http://www.w3.org/2000/svg'><circle r='4'/></svg>"}},
        ]},
    ],
}).encode()


async def test_formulas_become_mathml(session, client):
    """Формулы рисует браузер сам: MathML вместо доллара с исходником."""
    await _teacher(client)
    await client.post("/materials", files=_upload("lecture.ipynb", NB_MATH_AND_SVG))
    item = await session.scalar(select(Material))

    page = (await client.get(f"/materials/{item.id}/view")).text
    assert "<math" in page
    assert "$O(n \\log n)$" not in page          # исходник не показываем
    assert 'class="math-block"' in page          # выключная формула — отдельным блоком


async def test_math_cannot_smuggle_a_tag(session, client):
    """latex2mathml пропускает содержимое \\text{...} как есть — чистим сами."""
    await _teacher(client)
    await client.post("/materials", files=_upload("lecture.ipynb", NB_MATH_AND_SVG))
    item = await session.scalar(select(Material))

    page = (await client.get(f"/materials/{item.id}/view")).text
    assert "onerror" not in page
    assert "<img src=x" not in page


async def test_svg_output_is_shown_as_an_image(session, client):
    """SVG внутри <img> не исполняет скрипты и не ходит наружу — так и отдаём."""
    await _teacher(client)
    await client.post("/materials", files=_upload("lecture.ipynb", NB_MATH_AND_SVG))
    item = await session.scalar(select(Material))

    page = (await client.get(f"/materials/{item.id}/view")).text
    assert 'src="data:image/svg+xml;base64,' in page
    # В разметку страницы svg не попал: внутри <img> он безопасен, инлайном — нет.
    assert "circle r=" not in page


async def test_markdown_tables_are_rendered(session, client):
    """В конспектах есть таблицы: без правила table они шли столбиком палок."""
    await _teacher(client)
    table = "| Операция | Действие |\n|---|---|\n| `a + b` | сумма |\n"
    notebook = json.dumps({"cells": [{"cell_type": "markdown", "source": table}]}).encode()
    await client.post("/materials", files=_upload("lecture.ipynb", notebook))
    item = await session.scalar(select(Material))

    page = (await client.get(f"/materials/{item.id}/view")).text
    assert "<table>" in page and "<th>Операция</th>" in page
    assert "| Операция |" not in page
