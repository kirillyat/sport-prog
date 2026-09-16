"""Видимость разделов: что видит студент, что преподаватель, и кто это решает."""

from __future__ import annotations

from app.services import features


async def _login(client, name, teacher=False):
    data = {"name": name}
    if teacher:
        data["teacher"] = "true"
    await client.post("/login/dev", data=data)


async def _close(client, *, students: bool, teachers: bool):
    """Сохраняем форму админки: галочка есть — раздел открыт."""
    data = {}
    if students:
        data["materials:students"] = "on"
    if teachers:
        data["materials:teachers"] = "on"
    # Курс оставляем открытым, чтобы проверять именно материалы.
    data["course:students"] = "on"
    data["course:teachers"] = "on"
    return await client.post("/teacher/features", data=data)


async def test_sections_are_open_until_someone_closes_them(session, client):
    """Пустая таблица флагов — портал в полном составе."""
    await _login(client, "Аня")
    page = await client.get("/")
    assert 'href="/materials"' in page.text
    assert (await client.get("/materials")).status_code == 200


async def test_teacher_closes_materials_for_students_only(session, client):
    await _login(client, "Кирилл", teacher=True)
    await _close(client, students=False, teachers=True)

    # Преподаватель заходит по-прежнему.
    assert (await client.get("/materials")).status_code == 200
    assert 'href="/materials"' in (await client.get("/")).text

    await client.post("/logout")
    await _login(client, "Аня")
    page = await client.get("/")
    assert 'href="/materials"' not in page.text          # пункта в рейке нет
    closed = await client.get("/materials")
    assert "Раздел сейчас закрыт" in closed.text          # и по адресу тоже нет


async def test_closing_for_teachers_leaves_the_switch_reachable(session, client):
    """Закрыть раздел себе можно — вернуть его должно быть откуда."""
    await _login(client, "Кирилл", teacher=True)
    await _close(client, students=True, teachers=False)

    assert "Раздел сейчас закрыт" in (await client.get("/materials")).text
    back = await client.get("/teacher/features")
    assert back.status_code == 200 and "Видимость разделов" in back.text


async def test_student_cannot_touch_the_switches(session, client):
    await _login(client, "Кирилл", teacher=True)   # иначе Аня станет первой и получит роль
    await client.post("/logout")
    await _login(client, "Аня")
    assert "только для преподавателя" in (await client.get("/teacher/features")).text
    posted = await client.post("/teacher/features", data={})
    assert "только для преподавателя" in posted.text


async def test_flag_defaults_to_open_for_unknown_section(session, client):
    """Раздел, которого нет в реестре, не запирается случайно."""
    assert features.allows({}, "чего-то-нет", user=None) is True
