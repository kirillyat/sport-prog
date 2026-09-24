"""Ведомость группы: колонки, оценки, итоги и кто что видит.

Главные правила: колонка-задание считается сама и рукой не правится, студент
видит только свою строку, а раздел закрыт студентам, пока преподаватель
не откроет его сам.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import (
    Assignment,
    Attendance,
    FeatureFlag,
    Group,
    GroupMembership,
    Platform,
    Problem,
    ProblemSet,
    ProblemSetItem,
    Role,
    SheetColumn,
    SheetKind,
    SheetMark,
    SheetScale,
    Submission,
    User,
    utcnow,
)
from app.services import sheet


async def _login(client, name, teacher=False):
    data = {"name": name}
    if teacher:
        data["teacher"] = "true"
    await client.post("/login/dev", data=data)


@pytest.fixture
async def group(session):
    group = Group(title="ИИ-101", join_code="ai101")
    аня = User(display_name="Аня", role=Role.student, telegram_id=901)
    борис = User(display_name="Борис", role=Role.student, telegram_id=902)
    session.add_all([group, аня, борис])
    await session.commit()
    session.add_all([
        GroupMembership(group_id=group.id, user_id=аня.id),
        GroupMembership(group_id=group.id, user_id=борис.id),
    ])
    await session.commit()
    return group


async def _students(session, group):
    return await sheet.students_of(session, group)


async def test_manual_column_holds_a_grade_and_a_comment(session, group):
    column = SheetColumn(
        group_id=group.id, title="Устный приём 3", kind=SheetKind.manual,
        scale=SheetScale.pass_fail,
    )
    session.add(column)
    await session.commit()
    аня, борис = await _students(session, group)

    await sheet.put(session, column, аня.id, passed=True, comment="разобрала сортировку")
    await sheet.put(session, column, борис.id, passed=False)
    await session.commit()

    built = await sheet.build(session, group)
    assert built.value(аня.id, column.id).comment == "разобрала сортировку"
    assert built.value(борис.id, column.id).passed is False
    assert built.total(аня.id).passed == 1
    assert built.total(борис.id).passed == 0


async def test_an_empty_grade_is_not_the_same_as_a_failing_one(session, group):
    """«Не смотрел» и «незачёт» — разные вещи, и итог считает их по-разному."""
    column = SheetColumn(group_id=group.id, title="Домашка 3", kind=SheetKind.manual)
    session.add(column)
    await session.commit()
    аня, _ = await _students(session, group)

    built = await sheet.build(session, group)
    assert built.value(аня.id, column.id).empty is True

    await sheet.put(session, column, аня.id, passed=False)
    await session.commit()
    assert (await sheet.build(session, group)).value(аня.id, column.id).empty is False


async def test_clearing_a_grade_removes_the_row(session, group):
    column = SheetColumn(group_id=group.id, title="Домашка 3", kind=SheetKind.manual)
    session.add(column)
    await session.commit()
    аня, _ = await _students(session, group)

    await sheet.put(session, column, аня.id, passed=True)
    await session.commit()
    await sheet.put(session, column, аня.id, passed=None, comment="")
    await session.commit()

    assert (await session.execute(select(SheetMark))).scalars().all() == []


async def test_assignment_column_counts_itself(session, group):
    """Колонка-задание считается тем же правилом, что табло и матрица."""
    problem = Problem(
        platform=Platform.leetcode, external_id="two-sum", slug="two-sum", title="Сумма",
        url="https://leetcode.com/problems/two-sum/",
    )
    problem_set = ProblemSet(title="Неделя 3")
    session.add_all([problem, problem_set])
    await session.commit()
    session.add(ProblemSetItem(problem_set_id=problem_set.id, problem_id=problem.id, position=0))
    assignment = Assignment(
        title="Неделя 3", problem_set_id=problem_set.id, group_id=group.id
    )
    session.add(assignment)
    await session.commit()

    аня, борис = await _students(session, group)
    session.add(
        Submission(
            user_id=аня.id, platform=Platform.leetcode, external_id="s1", problem_id=problem.id,
            problem_slug="two-sum", problem_title="Сумма", verdict="Accepted",
            is_accepted=True, submitted_at=utcnow(),
        )
    )
    column = SheetColumn(
        group_id=group.id, title="Задачи недели 3", kind=SheetKind.assignment,
        assignment_id=assignment.id,
    )
    session.add(column)
    await session.commit()

    built = await sheet.build(session, group)
    assert (built.value(аня.id, column.id).solved, built.value(аня.id, column.id).total) == (1, 1)
    assert built.value(аня.id, column.id).passed is True
    assert built.value(борис.id, column.id).passed is False


async def test_assignment_column_is_not_editable_by_hand(session, client, group):
    """Иначе у портала появятся две правды об одном и том же."""
    problem_set = ProblemSet(title="Неделя 3")
    session.add(problem_set)
    await session.commit()
    assignment = Assignment(title="Неделя 3", problem_set_id=problem_set.id, group_id=group.id)
    session.add(assignment)
    await session.commit()
    column = SheetColumn(
        group_id=group.id, title="Задачи", kind=SheetKind.assignment, assignment_id=assignment.id
    )
    session.add(column)
    await session.commit()

    await _login(client, "Кирилл", teacher=True)
    page = await client.post(f"/teacher/sheet/{column.id}", data={})

    assert "руками её не ставят" in page.text
    assert (await session.execute(select(SheetMark))).scalars().all() == []


async def test_attendance_counts_lessons_and_forgives_excused(session, group):
    """Уважительная причина не пропуск, но и занятием не была."""
    columns = [
        SheetColumn(group_id=group.id, title=f"Занятие {n}", kind=SheetKind.attendance)
        for n in (1, 2, 3)
    ]
    session.add_all(columns)
    await session.commit()
    аня, _ = await _students(session, group)

    await sheet.put(session, columns[0], аня.id, attendance=Attendance.present)
    await sheet.put(session, columns[1], аня.id, attendance=Attendance.absent)
    await sheet.put(session, columns[2], аня.id, attendance=Attendance.excused)
    await session.commit()

    total = (await sheet.build(session, group)).total(аня.id)
    assert (total.present, total.lessons) == (1, 2)


async def test_points_and_pass_fail_are_counted_apart(session, group):
    """Сложить зачёты с баллами — значит взвесить их молча за преподавателя."""
    points = SheetColumn(
        group_id=group.id, title="Контрольная", kind=SheetKind.manual,
        scale=SheetScale.points, max_points=10,
    )
    marks = SheetColumn(group_id=group.id, title="Семинар", kind=SheetKind.manual)
    session.add_all([points, marks])
    await session.commit()
    аня, _ = await _students(session, group)

    await sheet.put(session, points, аня.id, points=7)
    await sheet.put(session, marks, аня.id, passed=True)
    await session.commit()

    total = (await sheet.build(session, group)).total(аня.id)
    assert (total.points, total.passed, total.gradable) == (7, 1, 1)


async def test_attendance_is_marked_from_the_lesson_page(session, client, group):
    column = SheetColumn(group_id=group.id, title="Занятие 1", kind=SheetKind.attendance)
    session.add(column)
    await session.commit()
    аня, борис = await _students(session, group)

    await _login(client, "Кирилл", teacher=True)
    await client.post(
        f"/teacher/sheet/{column.id}",
        data={f"mark:{аня.id}": "present", f"mark:{борис.id}": "absent"},
    )

    built = await sheet.build(session, group)
    assert built.value(аня.id, column.id).attendance == Attendance.present
    assert built.value(борис.id, column.id).attendance == Attendance.absent


async def test_a_student_sees_only_their_own_row(session, client, group):
    """Этим ведомость отличается от табло: там соревнование, здесь личные оценки."""
    column = SheetColumn(group_id=group.id, title="Устный приём", kind=SheetKind.manual)
    session.add(column)
    await session.commit()
    аня, борис = await _students(session, group)
    await sheet.put(session, column, аня.id, passed=True, comment="молодец")
    await sheet.put(session, column, борис.id, passed=False, comment="придёт ещё раз")
    await session.commit()
    # Раздел закрыт по умолчанию — для этой проверки открываем его студентам.
    session.add(FeatureFlag(key="grades", for_students=True, for_teachers=True))
    await session.commit()

    await client.post("/login/dev", data={"name": "Аня"})
    page = await client.get("/grades")

    assert page.status_code == 200
    assert "молодец" in page.text
    assert "придёт ещё раз" not in page.text
    assert "Борис" not in page.text


async def test_the_section_starts_closed_for_students(session, client, group):
    """Ведомость наполняется постепенно — показывать её раньше времени незачем."""
    session.add(FeatureFlag(key="grades", for_students=False, for_teachers=True))
    await session.commit()

    await client.post("/login/dev", data={"name": "Аня"})
    page = await client.get("/grades")

    assert "закрыт преподавателем" in page.text


async def test_copying_columns_takes_the_structure_but_not_the_grades(session, group):
    """Три группы ведут одно и то же — заводить колонки трижды незачем."""
    other = Group(title="ИИ-102", join_code="ai102")
    session.add(other)
    await session.commit()
    column = SheetColumn(group_id=group.id, title="Семинар 1", kind=SheetKind.manual)
    session.add(column)
    await session.commit()
    аня, _ = await _students(session, group)
    await sheet.put(session, column, аня.id, passed=True)
    await session.commit()

    added = await sheet.copy_columns(session, group.id, other.id)
    await session.commit()

    assert added == 1
    copied = (await sheet.columns_of(session, other.id))[0]
    assert copied.title == "Семинар 1"
    assert await sheet.marks_of(session, copied.id) == {}

    # Повторное копирование не удваивает колонки.
    assert await sheet.copy_columns(session, group.id, other.id) == 0


async def test_csv_carries_the_same_numbers_as_the_page(session, group):
    from app.services import export

    column = SheetColumn(
        group_id=group.id, title="Контрольная", kind=SheetKind.manual,
        scale=SheetScale.points, max_points=10,
    )
    session.add(column)
    await session.commit()
    аня, _ = await _students(session, group)
    await sheet.put(session, column, аня.id, points=7)
    await session.commit()

    text = export.sheet_csv(await sheet.build(session, group)).decode("utf-8-sig")
    assert "Контрольная" in text
    assert "7" in text and "Аня" in text
