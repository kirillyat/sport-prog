"""Свои задачи: страница задачи, прогон открытых тестов и сдача.

Страница одна на задачу: условие, открытые тесты, редактор и три кнопки.
Прогон открытых тестов ничего не стоит — он про «я правильно понял условие».
Сдача тратит попытку и идёт по всем тестам, включая закрытые.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.config import settings
from app.deps import CurrentUser, SessionDep
from app.i18n import translate as _
from app.models import Platform, Problem, Submission, Task, User, utcnow
from app.services import judge, notebook, solutions, tasks
from app.templating import templates

router = APIRouter(tags=["tasks"])


def _not_found() -> RedirectResponse:
    return RedirectResponse("/?err=" + quote(_("Задача не найдена")), status_code=303)


def _back(slug: str, message: str = "", error: str = "") -> RedirectResponse:
    query = ""
    if message:
        query = "?ok=" + quote(message)
    elif error:
        query = "?err=" + quote(error)
    return RedirectResponse(f"/tasks/{slug}{query}", status_code=303)


async def _page(
    request: Request,
    session: SessionDep,
    user: User,
    problem: Problem,
    task: Task,
    *,
    code: str = "",
    run: judge.RunResult | None = None,
    ok: str | None = None,
    error: str | None = None,
):
    """Страница задачи. Общая для GET и для прогона тестов, чтобы результат
    прогона можно было показать сразу, не храня его нигде между запросами."""
    used = await tasks.attempts_used(session, user.id, problem.id)
    history = await tasks.my_attempts(session, user.id, problem.id)
    assignment = await tasks.assignment_for(session, user.id, problem.id)
    return templates.TemplateResponse(
        request,
        "task.html",
        {
            "user": user,
            "problem": problem,
            "task": task,
            "assignment": assignment,
            "statement": notebook.render_markdown(task.statement),
            "open_tests": await tasks.tests_for(session, task, only_open=True),
            "attempts_used": used,
            "attempts_left": max(0, settings.task_attempts - used),
            "attempts_total": settings.task_attempts,
            "history": history,
            # Подключён и отвечает — разные вещи: виртуалку могли погасить
        # посреди контрольной, и студенту честнее сказать об этом сразу.
        "judge_ready": (await judge.config(session)).ready,
        "judge_down": not (await judge.health(session)).ok,
            # В редакторе — последнее присланное: начинать с чистого листа
            # после отказа хуже всего.
            "code": code or (history[0].code if history else "") or "",
            "run": run,
            "ok": ok if ok is not None else request.query_params.get("ok"),
            "error": error if error is not None else request.query_params.get("err"),
        },
    )


@router.get("/tasks/{slug}")
async def task_page(request: Request, session: SessionDep, user: CurrentUser, slug: str):
    found = await tasks.get_by_slug(session, slug)
    if found is None:
        return _not_found()
    return await _page(request, session, user, *found)


@router.post("/tasks/{slug}/run")
async def run_open_tests(
    request: Request, session: SessionDep, user: CurrentUser, slug: str, code: str = Form("")
):
    """Прогон по открытым тестам. Попытку не тратит."""
    found = await tasks.get_by_slug(session, slug)
    if found is None:
        return _not_found()
    problem, task = found

    text = code.replace("\r\n", "\n").strip()
    if not text:
        return await _page(request, session, user, problem, task, error=_("Пустое решение"))

    open_tests = await tasks.tests_for(session, task, only_open=True)
    try:
        cfg = await judge.config(session)
        result = await judge.run(
            cfg, text, open_tests, task.time_limit_ms, task.memory_limit_mb
        )
    except judge.JudgeUnavailable as exc:
        await judge.note(session, str(exc))
        return await _page(
            request, session, user, problem, task, code=text,
            error=_("Проверка недоступна: %(why)s") % {"why": exc},
        )
    await judge.note(session)
    return await _page(request, session, user, problem, task, code=text, run=result)


@router.post("/tasks/{slug}/submit")
async def submit(
    request: Request, session: SessionDep, user: CurrentUser, slug: str, code: str = Form("")
):
    """Сдача: все тесты и минус одна попытка."""
    found = await tasks.get_by_slug(session, slug)
    if found is None:
        return _not_found()
    problem, task = found

    text = code.replace("\r\n", "\n").strip()
    if not text:
        return await _page(request, session, user, problem, task, error=_("Пустое решение"))

    used = await tasks.attempts_used(session, user.id, problem.id)
    if used >= settings.task_attempts:
        return await _page(
            request, session, user, problem, task, code=text, error=_("Попытки кончились")
        )

    verdict, accepted, result = _("Не проверено"), False, None
    cfg = await judge.config(session)
    if cfg.ready:
        try:
            all_tests = await tasks.tests_for(session, task)
            result = await judge.run(
                cfg, text, all_tests, task.time_limit_ms, task.memory_limit_mb
            )
        except judge.JudgeUnavailable as exc:
            # Попытку не тратим: студент не виноват, что судья лёг.
            await judge.note(session, str(exc))
            return await _page(
                request, session, user, problem, task, code=text,
                error=_("Проверка недоступна: %(why)s") % {"why": exc},
            )
        await judge.note(session)
        verdict, accepted = result.summary, result.passed

    attempt = used + 1
    session.add(
        Submission(
            user_id=user.id,
            platform=Platform.local,
            # Своя нумерация: у задачи портала нет внешнего идентификатора.
            external_id=f"{problem.slug}-{user.id}-{attempt}",
            problem_id=problem.id,
            problem_slug=problem.slug,
            problem_title=problem.title,
            verdict=verdict,
            is_accepted=accepted,
            language="python",
            submitted_at=utcnow(),
            code=text,
            code_fetched_at=utcnow(),
        )
    )
    await session.commit()

    left = settings.task_attempts - attempt
    message = (
        _("Решение принято")
        if accepted
        else _("Сдано: %(verdict)s. Попыток осталось: %(left)s")
        % {"verdict": verdict, "left": left}
    )
    return await _page(request, session, user, problem, task, code=text, run=result, ok=message)


@router.post("/tasks/{slug}/to-teacher")
async def send_to_teacher(
    request: Request, session: SessionDep, user: CurrentUser, slug: str, code: str = Form("")
):
    """Отправить код преподавателю — отдельно от сдачи, попытку не тратит."""
    found = await tasks.get_by_slug(session, slug)
    if found is None:
        return _not_found()
    problem, task = found

    text = code.replace("\r\n", "\n").strip()
    if not text:
        return await _page(request, session, user, problem, task, error=_("Пустое решение"))
    if len(text) > solutions.MAX_CHARS:
        return await _page(
            request, session, user, problem, task, code=text,
            error=_("Решение длиннее %(limit)s символов") % {"limit": solutions.MAX_CHARS},
        )

    assignment = await tasks.assignment_for(session, user.id, problem.id)
    if assignment is None:
        return await _page(
            request, session, user, problem, task, code=text,
            error=_("Задача не входит ни в одно твоё задание — отправлять некуда"),
        )

    await solutions.put(
        session,
        assignment_id=assignment.id,
        problem_id=problem.id,
        user_id=user.id,
        code=text,
    )
    return _back(slug, message=_("Решение отправлено преподавателю"))
