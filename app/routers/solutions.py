"""Присланные решения: студент кладёт файл, преподаватель его читает.

Показ файла тот же, что у материалов, — свой бедный рендер без чужого HTML:
решение пишет студент, а открывается оно на нашем домене под чужой сессией.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select

from app.deps import CurrentUser, SessionDep
from app.i18n import translate as _
from app.models import (
    Assignment,
    GroupMembership,
    Problem,
    ProblemSetItem,
    SolutionUpload,
    Submission,
    User,
)
from app.services import notebook, solutions
from app.templating import templates

router = APIRouter(tags=["solutions"])


async def _may_see_assignment(session: SessionDep, user, assignment: Assignment) -> bool:
    if user.is_teacher or assignment.user_id == user.id:
        return True
    if assignment.group_id is None:
        return assignment.user_id is None
    member = await session.scalar(
        select(GroupMembership).where(
            GroupMembership.group_id == assignment.group_id,
            GroupMembership.user_id == user.id,
        )
    )
    return member is not None


@router.post("/assignments/{assignment_id}/solutions/{problem_id}")
async def upload_solution(
    session: SessionDep,
    user: CurrentUser,
    assignment_id: int,
    problem_id: int,
    file: UploadFile = File(...),
):
    back = f"/assignments/{assignment_id}"

    def fail(message: str) -> RedirectResponse:
        return RedirectResponse(f"{back}?err={quote(message)}", status_code=303)

    assignment = await session.get(Assignment, assignment_id)
    if assignment is None or not await _may_see_assignment(session, user, assignment):
        return fail(_("Задание не найдено"))
    if not assignment.requires_solution:
        return fail(_("Это задание решения файлом не требует"))

    # Задача должна быть из этого задания, иначе решение повиснет ни к чему.
    in_set = await session.scalar(
        select(ProblemSetItem).where(
            ProblemSetItem.problem_set_id == assignment.problem_set_id,
            ProblemSetItem.problem_id == problem_id,
        )
    )
    if in_set is None:
        return fail(_("Такой задачи в задании нет"))

    name = solutions.safe_filename(file.filename or "")
    if not solutions.is_allowed(name):
        return fail(
            _("Такой файл не принимаем. Можно: %(list)s") % {"list": solutions.EXTENSIONS_HINT}
        )
    data = await file.read()
    if not data:
        return fail(_("Файл пустой"))
    if len(data) > solutions.MAX_BYTES:
        return fail(_("Файл больше %(mb)s МБ") % {"mb": solutions.MAX_BYTES // 1024 // 1024})

    await solutions.put(
        session,
        assignment_id=assignment_id,
        problem_id=problem_id,
        user_id=user.id,
        filename=name,
        data=data,
    )
    return RedirectResponse(
        f"{back}?ok=" + quote(_("Решение отправлено на проверку")), status_code=303
    )


async def _upload_for(session: SessionDep, user, upload_id: int) -> SolutionUpload | None:
    upload = await session.get(SolutionUpload, upload_id)
    if upload is None:
        return None
    # Своё решение видит автор, чужое — только преподаватель.
    return upload if (user.is_teacher or upload.user_id == user.id) else None


@router.get("/solutions/{upload_id}/download")
async def download_solution(session: SessionDep, user: CurrentUser, upload_id: int):
    upload = await _upload_for(session, user, upload_id)
    if upload is None:
        return RedirectResponse("/?err=" + quote(_("Решение не найдено")), status_code=303)
    path = solutions.path_for(upload.stored_name)
    if not path.is_file():
        return RedirectResponse("/?err=" + quote(_("Файл потерялся на диске")), status_code=303)
    return FileResponse(
        path,
        media_type=solutions.content_type_for(upload.filename),
        filename=upload.filename,
        content_disposition_type="attachment",
    )


@router.get("/solutions/{upload_id}")
async def view_solution(request: Request, session: SessionDep, user: CurrentUser, upload_id: int):
    upload = await _upload_for(session, user, upload_id)
    if upload is None:
        return RedirectResponse("/?err=" + quote(_("Решение не найдено")), status_code=303)

    path = solutions.path_for(upload.stored_name)
    cells = text = rendered = code = None
    if path.is_file():
        content = path.read_bytes()
        name = upload.filename.lower()
        cells = notebook.parse(content) if name.endswith(".ipynb") else None
        if cells is None:
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                text = _("Файл не читается как текст — скачай его.")
            else:
                if name.endswith(".md"):
                    rendered, text = notebook.render_markdown(text), None
                else:
                    code, text = notebook.highlight_file(text, name), None

    author = await session.get(type(user), upload.user_id)
    assignment = await session.get(Assignment, upload.assignment_id)
    problem = await session.get(Problem, upload.problem_id)
    return templates.TemplateResponse(
        request,
        "solution.html",
        {
            "user": user,
            "upload": upload,
            "author": author,
            "assignment": assignment,
            "problem": problem,
            "cells": cells,
            "text": text,
            "rendered": rendered,
            "code": code,
            "ok": request.query_params.get("ok"),
            "error": request.query_params.get("err"),
        },
    )


@router.get("/submissions/{submission_id}")
async def view_submission(
    request: Request, session: SessionDep, user: CurrentUser, submission_id: int
):
    """Код посылки, если он уже загружен скриптом с машины преподавателя."""
    submission = await session.get(Submission, submission_id)
    if submission is None or (not user.is_teacher and submission.user_id != user.id):
        return RedirectResponse("/?err=" + quote(_("Посылка не найдена")), status_code=303)
    if not submission.code:
        return RedirectResponse(
            "/?err=" + quote(_("Исходник этой посылки не загружен")), status_code=303
        )

    author = await session.get(User, submission.user_id)
    problem = await session.get(Problem, submission.problem_id) if submission.problem_id else None
    name = f"solution.{(submission.language or 'txt').lower()}"
    return templates.TemplateResponse(
        request,
        "submission.html",
        {
            "user": user,
            "submission": submission,
            "author": author,
            "problem": problem,
            "code": notebook.highlight_file(submission.code, name),
        },
    )


@router.post("/solutions/{upload_id}/review")
async def review_solution(
    session: SessionDep,
    user: CurrentUser,
    upload_id: int,
    decision: str = Form(...),
    comment: str = Form(""),
):
    if not user.is_teacher:
        return RedirectResponse(
            "/?err=" + quote(_("Проверять решения может преподаватель")), status_code=303
        )
    upload = await session.get(SolutionUpload, upload_id)
    if upload is None:
        return RedirectResponse(
            "/teacher/reviews?err=" + quote(_("Решение не найдено")), status_code=303
        )

    from app.models import ReviewStatus

    status = ReviewStatus.accepted if decision == "accept" else ReviewStatus.rejected
    if status == ReviewStatus.rejected and not comment.strip():
        return RedirectResponse(
            f"/solutions/{upload_id}?err=" + quote(_("Отклонять без объяснения нельзя")),
            status_code=303,
        )
    await solutions.review(session, upload, status=status, comment=comment, reviewer_id=user.id)
    message = _("Решение принято") if status == ReviewStatus.accepted else _("Решение отклонено")
    return RedirectResponse(f"/teacher/reviews?ok={quote(message)}", status_code=303)
