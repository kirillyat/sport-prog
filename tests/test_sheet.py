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


async def test_every_sheet_page_renders_with_a_dated_lesson(session, client, group):
    """Страницы должны отрисовываться с заполненной колонкой, а не только с пустой.

    Дата занятия ломала три страницы разом: шаблон звал фильтр, которого нет,
    а проверки до этого заводили колонки без даты и ошибки не видели.
    """
    from datetime import UTC, datetime

    session.add(FeatureFlag(key="grades", for_students=True, for_teachers=True))
    seminar = await sheet.add_lesson(
        session, group.id, "Семинар 1",
        held_on=datetime(2026, 9, 24, 10, 30, tzinfo=UTC),
    )
    exam = SheetColumn(
        group_id=group.id, title="Контрольная", kind=SheetKind.manual,
        scale=SheetScale.points, max_points=10,
    )
    session.add(exam)
    await session.commit()
    columns = [c for c in await sheet.columns_of(session, group.id) if c.lesson_id == seminar.id]
    lesson = next(c for c in columns if c.kind == SheetKind.attendance)
    аня, _ = await _students(session, group)
    await sheet.put(session, lesson, аня.id, attendance=Attendance.excused)
    await sheet.put(session, exam, аня.id, points=7, comment="хорошо")
    await session.commit()

    await _login(client, "Кирилл", teacher=True)
    for path in (
        f"/teacher/groups/{group.id}/sheet",
        f"/teacher/groups/{group.id}/sheet/columns",
        f"/teacher/groups/{group.id}/sheet.csv",
        f"/teacher/sheet/{lesson.id}",
        f"/teacher/sheet/{exam.id}",
    ):
        assert (await client.get(path)).status_code == 200, path

    await client.post("/logout")
    await _login(client, "Аня")
    page = await client.get("/grades")
    assert page.status_code == 200
    assert "24.09.2026" in page.text


async def test_a_lesson_gets_all_its_grades_at_once(session, group):
    """За семинар оценок несколько — заводить их по одной было бы сорок пять форм."""
    lesson = await sheet.add_lesson(session, group.id, "Семинар 1")
    await session.commit()

    columns = [c for c in await sheet.columns_of(session, group.id) if c.lesson_id == lesson.id]
    assert [c.title for c in columns] == ["посещение", "работа на семинаре", "домашка"]
    assert [c.kind for c in columns] == [
        SheetKind.attendance, SheetKind.manual, SheetKind.manual
    ]


async def test_a_lesson_can_be_trimmed_to_what_actually_happened(session, group):
    """Лекция без домашки — обычное дело, лишнюю колонку навязывать незачем."""
    lesson = await sheet.add_lesson(session, group.id, "Лекция 2", parts=("attendance",))
    await session.commit()

    columns = [c for c in await sheet.columns_of(session, group.id) if c.lesson_id == lesson.id]
    assert [c.title for c in columns] == ["посещение"]


async def test_the_sheet_keeps_lessons_together_and_standalone_columns_last(session, group):
    """Колонки идут занятиями, а контрольная и проект — в конце, отдельным блоком."""
    await sheet.add_lesson(session, group.id, "Семинар 1", parts=("attendance", "homework"))
    session.add(
        SheetColumn(group_id=group.id, title="Контрольная", kind=SheetKind.manual)
    )
    await session.commit()
    await sheet.add_lesson(session, group.id, "Семинар 2", parts=("attendance",))
    await session.commit()

    built = await sheet.build(session, group)
    assert [block.title for block in built.blocks] == ["Семинар 1", "Семинар 2", ""]
    assert [len(block.columns) for block in built.blocks] == [2, 1, 1]
    assert built.blocks[-1].lesson is None


async def test_deleting_a_lesson_takes_its_columns_and_marks(session, group):
    lesson = await sheet.add_lesson(session, group.id, "Семинар 1")
    await session.commit()
    column = [c for c in await sheet.columns_of(session, group.id) if c.lesson_id == lesson.id][0]
    аня, _ = await _students(session, group)
    await sheet.put(session, column, аня.id, attendance=Attendance.absent)
    await session.commit()

    await sheet.remove_lesson(session, lesson)
    await session.commit()

    assert await sheet.columns_of(session, group.id) == []
    assert (await session.execute(select(SheetMark))).scalars().all() == []


async def test_copying_carries_lessons_but_not_their_dates(session, group):
    """У другой группы тот же семинар в другой день — чужую дату не переносим."""
    from datetime import UTC, datetime

    other = Group(title="ИИ-102", join_code="ai102")
    session.add(other)
    await sheet.add_lesson(
        session, group.id, "Семинар 1", held_on=datetime(2026, 9, 24, 10, 30, tzinfo=UTC)
    )
    await session.commit()

    await sheet.copy_columns(session, group.id, other.id)
    await session.commit()

    lessons = await sheet.lessons_of(session, other.id)
    assert [lesson.title for lesson in lessons] == ["Семинар 1"]
    assert lessons[0].held_on is None
    copied = await sheet.columns_of(session, other.id)
    assert len(copied) == 3 and all(c.lesson_id == lessons[0].id for c in copied)


async def test_a_lesson_is_created_from_the_page_with_chosen_parts(session, client, group):
    await _login(client, "Кирилл", teacher=True)
    page = await client.post(
        f"/teacher/groups/{group.id}/sheet/lessons",
        data={"title": "Семинар 4", "part:attendance": "on", "part:homework": "on"},
    )

    assert "Занятие добавлено" in page.text
    columns = await sheet.columns_of(session, group.id)
    assert [c.title for c in columns] == ["посещение", "домашка"]


async def test_a_lesson_without_any_grade_is_refused(session, client, group):
    """Занятие без единой оценки — пустая шапка в ведомости и ничего больше."""
    await _login(client, "Кирилл", teacher=True)
    page = await client.post(
        f"/teacher/groups/{group.id}/sheet/lessons", data={"title": "Семинар 5"}
    )

    assert "хотя бы одну" in page.text
    assert await sheet.lessons_of(session, group.id) == []


async def test_an_unmarked_lesson_is_not_a_miss(session, group):
    """Семестр заводят вперёд: «посещено 0 из 15» в сентябре — неправда."""
    await sheet.add_lesson(session, group.id, "Семинар 1", parts=("attendance",))
    await sheet.add_lesson(session, group.id, "Семинар 2", parts=("attendance",))
    await session.commit()
    аня, _ = await _students(session, group)

    total = (await sheet.build(session, group)).total(аня.id)
    assert (total.present, total.lessons) == (0, 0)

    первый = (await sheet.columns_of(session, group.id))[0]
    await sheet.put(session, первый, аня.id, attendance=Attendance.present)
    await session.commit()

    total = (await sheet.build(session, group)).total(аня.id)
    assert (total.present, total.lessons) == (1, 1)


async def test_a_lesson_can_be_renamed_and_dated_afterwards(session, client, group):
    """Семестр заводят вперёд, а расписание уточняется — дата вписывается потом."""
    lesson = await sheet.add_lesson(session, group.id, "Семинар 1", parts=("attendance",))
    await session.commit()

    await _login(client, "Кирилл", teacher=True)
    page = await client.post(
        f"/teacher/sheet/lessons/{lesson.id}",
        data={"title": "Семинар 1 · два указателя", "held_on": "2026-09-24T10:30"},
    )

    assert "Занятие изменено" in page.text
    await session.refresh(lesson)
    assert lesson.title == "Семинар 1 · два указателя"
    assert lesson.held_on is not None


async def test_clearing_the_date_leaves_the_lesson_alone(session, client, group):
    """Пустое поле снимает дату, а не молча оставляет прежнюю."""
    from datetime import UTC, datetime

    lesson = await sheet.add_lesson(
        session, group.id, "Семинар 1", held_on=datetime(2026, 9, 24, tzinfo=UTC),
        parts=("attendance",),
    )
    await session.commit()

    await _login(client, "Кирилл", teacher=True)
    await client.post(
        f"/teacher/sheet/lessons/{lesson.id}", data={"title": "Семинар 1", "held_on": ""}
    )

    await session.refresh(lesson)
    assert lesson.held_on is None
    assert (await sheet.columns_of(session, group.id)) != []


async def test_editing_does_not_touch_the_grades(session, client, group):
    lesson = await sheet.add_lesson(session, group.id, "Семинар 1", parts=("attendance",))
    await session.commit()
    column = (await sheet.columns_of(session, group.id))[0]
    аня, _ = await _students(session, group)
    await sheet.put(session, column, аня.id, attendance=Attendance.present)
    await session.commit()

    await _login(client, "Кирилл", teacher=True)
    await client.post(
        f"/teacher/sheet/lessons/{lesson.id}", data={"title": "Семинар первый", "held_on": ""}
    )

    built = await sheet.build(session, group)
    assert built.value(аня.id, column.id).attendance == Attendance.present


async def test_percent_is_counted_from_the_whole_course(session, group):
    """Иначе первая же пятёрка из пяти покажет сто процентов за весь семестр."""
    первая = SheetColumn(
        group_id=group.id, title="Контрольная 1", kind=SheetKind.manual,
        scale=SheetScale.points, max_points=10,
    )
    вторая = SheetColumn(
        group_id=group.id, title="Контрольная 2", kind=SheetKind.manual,
        scale=SheetScale.points, max_points=10,
    )
    session.add_all([первая, вторая])
    await session.commit()
    аня, _ = await _students(session, group)

    await sheet.put(session, первая, аня.id, points=10)
    await session.commit()

    total = (await sheet.build(session, group)).total(аня.id)
    assert (total.points, total.points_max, total.points_percent) == (10, 20, 50)


async def test_percent_is_absent_rather_than_zero_when_there_is_nothing_to_count(
    session, group
):
    """Ноль процентов и «нечего считать» — разные вещи, и на экране тоже."""
    аня, _ = await _students(session, group)
    total = (await sheet.build(session, group)).total(аня.id)

    assert total.points_percent is None
    assert total.passed_percent is None
    assert total.present_percent is None


async def test_the_student_screen_shows_percents_and_a_dot_per_lesson(session, client, group):
    session.add(FeatureFlag(key="grades", for_students=True, for_teachers=True))
    await sheet.add_lesson(session, group.id, "Семинар 1", parts=("attendance",))
    await sheet.add_lesson(session, group.id, "Семинар 2", parts=("attendance",))
    exam = SheetColumn(
        group_id=group.id, title="Контрольная", kind=SheetKind.manual,
        scale=SheetScale.points, max_points=10,
    )
    session.add(exam)
    await session.commit()
    аня, _ = await _students(session, group)
    первое, второе = [
        c for c in await sheet.columns_of(session, group.id) if c.kind == SheetKind.attendance
    ]
    await sheet.put(session, первое, аня.id, attendance=Attendance.present)
    await sheet.put(session, второе, аня.id, attendance=Attendance.absent)
    await sheet.put(session, exam, аня.id, points=7)
    await session.commit()

    await client.post("/login/dev", data={"name": "Аня"})
    page = await client.get("/grades")

    assert page.status_code == 200
    assert "70" in page.text and "50" in page.text          # баллы и посещаемость в процентах
    assert page.text.count('class="dot present"') == 1
    assert page.text.count('class="dot absent"') == 1


async def test_untouched_works_are_counted_apart_from_failed_ones(session, group):
    """«2 из 7» выглядит провалом, если пять работ просто ждут проверки."""
    сдана = SheetColumn(group_id=group.id, title="Домашка 1", kind=SheetKind.manual)
    ждёт = SheetColumn(group_id=group.id, title="Домашка 2", kind=SheetKind.manual)
    не_зачтена = SheetColumn(group_id=group.id, title="Домашка 3", kind=SheetKind.manual)
    session.add_all([сдана, ждёт, не_зачтена])
    await session.commit()
    аня, _ = await _students(session, group)
    await sheet.put(session, сдана, аня.id, passed=True)
    await sheet.put(session, не_зачтена, аня.id, passed=False)
    await session.commit()

    total = (await sheet.build(session, group)).total(аня.id)
    assert (total.passed, total.gradable, total.ungraded) == (1, 3, 1)
