"""Раздел «Курс»: недели семестра, конспекты, практики и домашние задания.

Содержимое приезжает из репозитория преподавателей (`scripts/sync_course.py`)
и лежит файлами, поэтому здесь нет ни моделей, ни прав по группам: курс видят
все, кто вошёл на портал.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, RedirectResponse

from app.deps import CurrentUser
from app.services import course, materials, notebook
from app.templating import templates

router = APIRouter(tags=["course"])


def _back(error: str) -> RedirectResponse:
    return RedirectResponse("/course?err=" + quote(error), status_code=303)


@router.get("/course")
async def course_index(request: Request, user: CurrentUser):
    return templates.TemplateResponse(
        request,
        "course.html",
        {
            "user": user,
            "weeks": course.weeks(),
            "error": request.query_params.get("err"),
        },
    )


@router.get("/course/{name:str}")
async def course_page(request: Request, user: CurrentUser, name: str):
    found = course.page(name)
    if found is None:
        return _back("Такой страницы у курса нет")
    title, body = found
    return templates.TemplateResponse(
        request,
        "course_page.html",
        {"user": user, "title": title, "rendered": notebook.render_markdown(body)},
    )


@router.get("/course/{semester}/{number}")
async def course_week(request: Request, user: CurrentUser, semester: str, number: str):
    week = course.week(semester, number)
    if week is None:
        return _back("Неделя не найдена")
    return templates.TemplateResponse(
        request,
        "course_week.html",
        {"user": user, "week": week, "rendered": notebook.render_markdown(week.body)},
    )


@router.get("/course/{semester}/{number}/{slot}")
async def course_slot(
    request: Request, user: CurrentUser, semester: str, number: str, slot: str
):
    week = course.week(semester, number)
    if week is None:
        return _back("Неделя не найдена")
    found = next((s for s in week.slots if s.key == slot), None)
    if found is None:
        return _back("В этой неделе такого нет")

    path = course.file_path(semester, number, found.filename)
    cells = notebook.parse(path.read_bytes()) if path else None
    return templates.TemplateResponse(
        request,
        "course_view.html",
        {"user": user, "week": week, "slot": found, "cells": cells},
    )


@router.get("/course/{semester}/{number}/file/{name}")
async def course_file(user: CurrentUser, semester: str, number: str, name: str):
    path = course.file_path(semester, number, name)
    if path is None:
        return _back("Файл не найден")
    # Отдаём вложением: что бы ни лежало в папке недели, на нашем домене оно
    # открываться не должно.
    return FileResponse(
        path,
        media_type=materials.content_type_for(path.name),
        filename=path.name,
        content_disposition_type="attachment",
    )
