"""Ведомость группы: страницы преподавателя и своя строка у студента.

Разделение простое: преподаватель видит всю группу, студент — только себя.
Именно этим ведомость отличается от табло, где видно всех: табло про
соревнование, ведомость про личные оценки.

Оценки проставляются не в общей таблице, а на странице колонки: одна форма
на всю группу, одна кнопка. Ведомость при двадцати студентах и пятнадцати
работах слишком широка, чтобы попадать в нужную клетку, — особенно
с телефона на паре.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from app.deps import CurrentUser, SessionDep, TeacherUser
from app.i18n import translate as _
from app.models import (
    Assignment,
    Attendance,
    Group,
    SheetColumn,
    SheetKind,
    SheetScale,
    utcnow,
)
from app.services import export, sheet
from app.templating import parse_local_input, templates

router = APIRouter(tags=["sheet"])


def _redirect(path: str, message: str = "", error: str = "") -> RedirectResponse:
    query = ""
    if message:
        query = "?ok=" + quote(message)
    elif error:
        query = "?err=" + quote(error)
    return RedirectResponse(f"{path}{query}", status_code=303)


def _flash(request: Request) -> dict:
    return {
        "ok": request.query_params.get("ok"),
        "error": request.query_params.get("err"),
    }


def _number(raw: str) -> float | None:
    try:
        return float(raw.replace(",", ".").strip())
    except (AttributeError, ValueError):
        return None


# ------------------------------------------------------------- преподаватель


@router.get("/teacher/groups/{group_id}/sheet")
async def group_sheet(request: Request, session: SessionDep, user: TeacherUser, group_id: int):
    group = await session.get(Group, group_id)
    if group is None:
        return _redirect("/teacher/groups", error=_("Группа не найдена"))
    return templates.TemplateResponse(
        request,
        "teacher/sheet.html",
        {
            "user": user,
            "group": group,
            "sheet": await sheet.build(session, group),
            **_flash(request),
        },
    )


@router.get("/teacher/groups/{group_id}/sheet.csv")
async def group_sheet_csv(session: SessionDep, user: TeacherUser, group_id: int):
    group = await session.get(Group, group_id)
    if group is None:
        return _redirect("/teacher/groups", error=_("Группа не найдена"))
    built = await sheet.build(session, group)
    return Response(
        content=export.sheet_csv(built),
        headers=export.response_headers(export.filename(f"sheet-{group.id}", utcnow())),
    )


@router.get("/teacher/groups/{group_id}/sheet/columns")
async def sheet_columns(request: Request, session: SessionDep, user: TeacherUser, group_id: int):
    from sqlalchemy import select

    group = await session.get(Group, group_id)
    if group is None:
        return _redirect("/teacher/groups", error=_("Группа не найдена"))
    assignments = list(
        (
            await session.execute(
                select(Assignment)
                .where((Assignment.group_id == group_id) | (Assignment.group_id.is_(None)))
                .order_by(Assignment.assigned_at.desc())
            )
        )
        .scalars()
        .all()
    )
    others = list(
        (
            await session.execute(
                select(Group).where(Group.id != group_id).order_by(Group.title)
            )
        )
        .scalars()
        .all()
    )
    return templates.TemplateResponse(
        request,
        "teacher/sheet_columns.html",
        {
            "user": user,
            "group": group,
            "columns": await sheet.columns_of(session, group_id),
            "assignments": assignments,
            "others": others,
            **_flash(request),
        },
    )


@router.post("/teacher/groups/{group_id}/sheet/columns")
async def add_column(
    session: SessionDep,
    user: TeacherUser,
    group_id: int,
    title: str = Form(...),
    kind: str = Form("manual"),
    scale: str = Form("pass_fail"),
    max_points: str = Form("1"),
    assignment_id: str = Form(""),
    required_solved: str = Form(""),
    held_on: str = Form(""),
):
    group = await session.get(Group, group_id)
    if group is None:
        return _redirect("/teacher/groups", error=_("Группа не найдена"))
    back = f"/teacher/groups/{group_id}/sheet/columns"
    if not title.strip():
        return _redirect(back, error=_("Пустое название"))

    try:
        column_kind = SheetKind(kind)
        column_scale = SheetScale(scale)
    except ValueError:
        return _redirect(back, error=_("Неизвестный вид колонки"))

    linked = int(assignment_id) if assignment_id.strip() else None
    if column_kind == SheetKind.assignment:
        if linked is None or await session.get(Assignment, linked) is None:
            return _redirect(back, error=_("Выбери задание, из которого считать"))
    else:
        linked = None
    # Посещаемость — всегда отметка, а не балл: считать явку баллами
    # значит смешать её с работой, а вес у них разный.
    if column_kind == SheetKind.attendance:
        column_scale = SheetScale.pass_fail

    existing = await sheet.columns_of(session, group_id)
    session.add(
        SheetColumn(
            group_id=group_id,
            title=title.strip(),
            kind=column_kind,
            scale=column_scale,
            max_points=max(0.0, _number(max_points) or 1),
            position=len(existing),
            assignment_id=linked,
            required_solved=int(required_solved) if required_solved.strip().isdigit() else None,
            held_on=parse_local_input(held_on) if held_on.strip() else None,
        )
    )
    await session.commit()
    return _redirect(back, message=_("Колонка добавлена"))


@router.post("/teacher/sheet/{column_id}/delete")
async def delete_column(session: SessionDep, user: TeacherUser, column_id: int):
    column = await session.get(SheetColumn, column_id)
    if column is None:
        return _redirect("/teacher/groups", error=_("Колонка не найдена"))
    group_id = column.group_id
    await sheet.remove(session, column)
    await session.commit()
    return _redirect(
        f"/teacher/groups/{group_id}/sheet/columns", message=_("Колонка удалена вместе с оценками")
    )


@router.post("/teacher/groups/{group_id}/sheet/copy")
async def copy_columns(
    session: SessionDep, user: TeacherUser, group_id: int, source_id: int = Form(...)
):
    back = f"/teacher/groups/{group_id}/sheet/columns"
    if await session.get(Group, source_id) is None:
        return _redirect(back, error=_("Группа не найдена"))
    added = await sheet.copy_columns(session, source_id, group_id)
    await session.commit()
    return _redirect(back, message=_("Скопировано колонок: %(count)s") % {"count": added})


@router.get("/teacher/sheet/{column_id}")
async def column_page(request: Request, session: SessionDep, user: TeacherUser, column_id: int):
    """Проставить оценки или отметить занятие — смотря какая колонка."""
    column = await session.get(SheetColumn, column_id)
    if column is None:
        return _redirect("/teacher/groups", error=_("Колонка не найдена"))
    group = await session.get(Group, column.group_id)
    if column.kind == SheetKind.assignment:
        return _redirect(
            f"/teacher/groups/{group.id}/sheet",
            error=_("Эта колонка считается из задания — руками её не ставят"),
        )
    page = "teacher/sheet_attendance.html" if column.kind == SheetKind.attendance \
        else "teacher/sheet_marks.html"
    return templates.TemplateResponse(
        request,
        page,
        {
            "user": user,
            "group": group,
            "column": column,
            "students": await sheet.students_of(session, group),
            "marks": await sheet.marks_of(session, column.id),
            "attendance": Attendance,
            **_flash(request),
        },
    )


@router.post("/teacher/sheet/{column_id}")
async def save_column(request: Request, session: SessionDep, user: TeacherUser, column_id: int):
    """Одна форма на всю группу: оценки приходят разом, а не по одной."""
    column = await session.get(SheetColumn, column_id)
    if column is None:
        return _redirect("/teacher/groups", error=_("Колонка не найдена"))
    if column.kind == SheetKind.assignment:
        return _redirect(
            f"/teacher/groups/{column.group_id}/sheet",
            error=_("Эта колонка считается из задания — руками её не ставят"),
        )

    form = await request.form()
    group = await session.get(Group, column.group_id)
    for student in await sheet.students_of(session, group):
        key = str(student.id)
        comment = str(form.get(f"comment:{key}", "") or "")
        if column.kind == SheetKind.attendance:
            raw = str(form.get(f"mark:{key}", "") or "")
            state = Attendance(raw) if raw in set(Attendance) else None
            await sheet.put(
                session, column, student.id,
                attendance=state, comment=comment, graded_by_id=user.id,
            )
            continue
        if column.scale == SheetScale.points:
            points = _number(str(form.get(f"mark:{key}", "") or ""))
            await sheet.put(
                session, column, student.id,
                points=points, comment=comment, graded_by_id=user.id,
            )
            continue
        # Зачёт/незачёт: галочки приходят только для отмеченных, но «незачёт»
        # тоже решение преподавателя — его нужно отличать от «ещё не смотрел».
        raw = str(form.get(f"mark:{key}", "") or "")
        passed = {"yes": True, "no": False}.get(raw)
        await sheet.put(
            session, column, student.id,
            passed=passed, comment=comment, graded_by_id=user.id,
        )
    await session.commit()
    return _redirect(f"/teacher/groups/{group.id}/sheet", message=_("Оценки сохранены"))


# ------------------------------------------------------------------- студент


@router.get("/grades")
async def my_grades(request: Request, session: SessionDep, user: CurrentUser):
    """Своя строка ведомости. Чужих оценок здесь нет и быть не может."""
    return templates.TemplateResponse(
        request,
        "grades.html",
        {
            "user": user,
            "rows": await sheet.my_rows(session, user),
            "kinds": SheetKind,
            "scales": SheetScale,
            "attendance": Attendance,
            **_flash(request),
        },
    )
