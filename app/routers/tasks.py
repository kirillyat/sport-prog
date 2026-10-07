"""Свои задачи: страница задачи, прогон открытых тестов и сдача.

Страница одна на задачу: условие, открытые тесты, редактор и две кнопки.
Прогон открытых тестов ничего не стоит — он про «я правильно понял условие».
Сдача тратит попытку, идёт по всем тестам, включая закрытые, и всегда
уходит преподавателю на проверку глазами: автопроверка не заменяет взгляда
на код, а отдельная кнопка «преподавателю» означала бы, что половина
решений до него не доедет.

Разговор с судьёй длится секунды, и всё это время портал не должен ничего
держать. Отсюда два правила ниже: соединение к базе отпускается перед
запросом к судье, а на человека приходится один прогон за раз. Без первого
пятнадцати одновременных сдач хватало, чтобы у остальных перестали
открываться страницы; без второго один студент занимал бы очередь судьи
на всю группу.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
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

# Кто прямо сейчас ждёт судью. Портал живёт одним процессом, поэтому
# обычного множества достаточно — ни Redis, ни блокировок в базе.
_running: set[int] = set()


class _Busy(Exception):
    """У этого студента уже идёт прогон."""


@asynccontextmanager
async def _one_at_a_time(user_id: int):
    if user_id in _running:
        raise _Busy
    _running.add(user_id)
    try:
        yield
    finally:
        _running.discard(user_id)


async def _judged(session: SessionDep, cfg, code: str, tests: list, limits: tuple[int, int]):
    """Прогон с отпущенным соединением к базе.

    Сессия держит соединение, пока открыта транзакция, а судья отвечает
    секундами. Пятнадцать таких ожиданий выбирали весь пул, и у остальных
    переставали открываться страницы — поэтому тесты и лимиты забираем
    заранее, а перед запросом транзакцию закрываем.

    Именно коммитом, хотя писать нечего: откат обнуляет уже загруженные
    строки, и первое же обращение к ним из шаблона полезло бы в базу
    посреди отрисовки. Коммит при `expire_on_commit=False` их сохраняет.
    """
    await session.commit()
    return await judge.run(cfg, code, tests, *limits)


def _solution(code: str) -> tuple[str, str]:
    """Нормализованный код и причина отказа, если он не годится.

    Длину ограничиваем и здесь, а не только при отправке преподавателю:
    иначе мегабайт из буфера обмена уедет к судье столько раз, сколько
    в задаче тестов.
    """
    text = code.replace("\r\n", "\n").strip()
    if not text:
        return "", _("Пустое решение")
    if len(text) > solutions.MAX_CHARS:
        return text, _("Решение длиннее %(limit)s символов") % {"limit": solutions.MAX_CHARS}
    return text, ""


def _not_found() -> RedirectResponse:
    return RedirectResponse("/?err=" + quote(_("Задача не найдена")), status_code=303)


def _epoch_ms(moment: datetime | None) -> int:
    """Метка для скрипта редактора: чей код новее — черновика или страницы.

    SQLite отдаёт время без зоны, хотя пишем мы UTC, — достраиваем зону сами.
    """
    if moment is None:
        return 0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return int(moment.timestamp() * 1000)


async def _page(
    request: Request,
    session: SessionDep,
    user: User,
    problem: Problem,
    task: Task,
    *,
    code: str | None = None,
    run: judge.RunResult | None = None,
    run_kind: str = "",
    sent_to_teacher: bool = False,
    ok: str | None = None,
    error: str | None = None,
):
    """Страница задачи. Общая для GET и для прогона тестов, чтобы результат
    прогона можно было показать сразу, не храня его нигде между запросами.

    `code` передаётся всегда, когда запрос его принёс, — в том числе при
    отказе. Студент пишет решение прямо здесь, и страница, вернувшаяся
    после кнопки без его кода, — это потерянная работа, а не сообщение
    об ошибке.
    """
    used = await tasks.attempts_used(session, user.id, problem.id)
    history = await tasks.my_attempts(session, user.id, problem.id)
    assignment = await tasks.assignment_for(session, user.id, problem.id)
    open_tests = await tasks.tests_for(session, task, only_open=True)
    if code is not None:
        shown, shown_at = code, utcnow()
    elif history:
        shown, shown_at = history[0].code or "", history[0].submitted_at
    else:
        shown, shown_at = "", None
    return templates.TemplateResponse(
        request,
        "task.html",
        {
            "user": user,
            "problem": problem,
            "task": task,
            "assignment": assignment,
            "statement": notebook.render_markdown(task.statement),
            "open_tests": open_tests,
            # Ввод и ответ открытого теста — к его строке в результатах прогона.
            "open_by_position": {test.position: test for test in open_tests},
            "attempts_used": used,
            "attempts_left": max(0, settings.task_attempts - used),
            "attempts_total": settings.task_attempts,
            "history": history,
            # Подключён и отвечает — разные вещи: виртуалку могли погасить
            # посреди контрольной, и студенту честнее сказать об этом сразу.
            "judge_ready": (await judge.config(session)).ready,
            "judge_down": not (await judge.health(session)).ok,
            # В редакторе — только что присланное, иначе последняя сдача:
            # начинать с чистого листа после отказа хуже всего. Время кода
            # нужно скрипту: черновик в браузере новее — побеждает черновик.
            "code": shown,
            "code_at": _epoch_ms(shown_at),
            "run": run,
            "run_kind": run_kind,
            "sent_to_teacher": sent_to_teacher,
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

    text, error = _solution(code)
    if error:
        return await _page(request, session, user, problem, task, code=code, error=error)

    cfg = await judge.config(session)
    open_tests = judge.cases(await tasks.tests_for(session, task, only_open=True))
    limits = (task.time_limit_ms, task.memory_limit_mb)
    try:
        async with _one_at_a_time(user.id):
            result = await _judged(session, cfg, text, open_tests, limits)
    except _Busy:
        return await _page(
            request, session, user, problem, task, code=text,
            error=_("Предыдущий прогон ещё идёт — подожди его конца"),
        )
    except judge.JudgeUnavailable as exc:
        await judge.note(session, str(exc))
        return await _page(
            request, session, user, problem, task, code=text,
            error=_("Проверка недоступна: %(why)s") % {"why": exc},
        )
    await judge.note(session)
    return await _page(
        request, session, user, problem, task, code=text, run=result, run_kind="run"
    )


@router.post("/tasks/{slug}/submit")
async def submit(
    request: Request, session: SessionDep, user: CurrentUser, slug: str, code: str = Form("")
):
    """Сдача: все тесты и минус одна попытка."""
    found = await tasks.get_by_slug(session, slug)
    if found is None:
        return _not_found()
    problem, task = found

    text, error = _solution(code)
    if error:
        return await _page(request, session, user, problem, task, code=code, error=error)

    used = await tasks.attempts_used(session, user.id, problem.id)
    if used >= settings.task_attempts:
        return await _page(
            request, session, user, problem, task, code=text, error=_("Попытки кончились")
        )

    verdict, accepted, result = _("Не проверено"), False, None
    cfg = await judge.config(session)
    if cfg.ready:
        all_tests = judge.cases(await tasks.tests_for(session, task))
        limits = (task.time_limit_ms, task.memory_limit_mb)
        try:
            async with _one_at_a_time(user.id):
                result = await _judged(session, cfg, text, all_tests, limits)
        except _Busy:
            return await _page(
                request, session, user, problem, task, code=text,
                error=_("Предыдущий прогон ещё идёт — подожди его конца"),
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

    # Сдача уходит преподавателю всегда, а не по отдельной кнопке. Задачи
    # вне задания отправлять некуда — посылка с кодом при этом всё равно есть.
    assignment = await tasks.assignment_for(session, user.id, problem.id)
    if assignment is not None:
        await solutions.put(
            session,
            assignment_id=assignment.id,
            problem_id=problem.id,
            user_id=user.id,
            code=text,
        )

    return await _page(
        request, session, user, problem, task, code=text, run=result, run_kind="submit",
        sent_to_teacher=assignment is not None,
        # Итог сдачи — в плашке результата рядом с редактором, а не во
        # всплывающей строке наверху страницы, которую не видно у длинного условия.
        ok="",
    )
