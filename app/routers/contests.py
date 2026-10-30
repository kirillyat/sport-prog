"""Раздел соревнований: `/contest`.

Отдельная половина портала на том же движке. Вход тот же, дизайн тот же,
а рейка своя: участнику соревнования не нужны ни оценки, ни материалы, ни
лента курса — только задачи, табло, посылки и команда.

Правила живут в `app/services/contests.py`; здесь только формы и страницы.
Сдача повторяет страницу своей задачи (`app/routers/tasks.py`) и берёт у неё
те же предохранители: соединение к базе отпускается перед судьёй, и
у команды одна посылка за раз — за одним ноутбуком сидят вместе.
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.deps import CurrentUser, OptionalInt, SessionDep, TeacherUser
from app.i18n import translate as _
from app.models import (
    Contest,
    ContestRules,
    ContestRun,
    ContestTeam,
    ProblemSet,
    Task,
    User,
    utcnow,
)
from app.routers.tasks import _Busy, _epoch_ms, _judged, _one_at_a_time, _solution
from app.services import contests, judge, languages, notebook, tasks
from app.services.contests import ContestError
from app.templating import parse_local_input, templates

router = APIRouter(prefix="/contest", tags=["contests"])


def _go(path: str, message: str | None = None, error: str | None = None) -> RedirectResponse:
    params = []
    if message:
        params.append("ok=" + quote(message))
    if error:
        params.append("err=" + quote(error))
    return RedirectResponse(path + ("?" + "&".join(params) if params else ""), status_code=303)


def _not_found() -> RedirectResponse:
    return _go("/contest", error=_("Соревнование не найдено"))


async def _contest(session: SessionDep, user: User, contest_id: int) -> Contest | None:
    contest = await session.get(Contest, contest_id)
    if contest is None or not contests.may_see(contest, user):
        return None
    return contest


def _base(request: Request, user: User, contest: Contest | None = None, **extra) -> dict:
    return {
        "user": user,
        "contest": contest,
        "ok": request.query_params.get("ok"),
        "error": request.query_params.get("err"),
        **extra,
    }


# ------------------------------------------------------------------ список


@router.get("")
async def contest_list(request: Request, session: SessionDep, user: CurrentUser):
    """Главная раздела. Идущее — крупно сверху: участнику во время тура не
    нужно искать своё соревнование в таблице. Ниже — куда можно записаться,
    прошедшие, а у преподавателя ещё черновики."""
    only_mine = request.query_params.get("mine") == "1"
    items = await contests.visible(session, user)
    mine = {}
    for item in items:
        team = await contests.team_of(session, item, user)
        if team is not None:
            mine[item.id] = team
    if only_mine:
        items = [item for item in items if item.id in mine]
    groups: dict[str, list[Contest]] = {
        "running": [], "registration": [], "finished": [], "draft": []
    }
    for item in items:
        groups[item.phase()].append(item)
    # Сначала то, где человек участвует: таких почти всегда одно.
    groups["running"].sort(key=lambda item: item.id not in mine)
    return templates.TemplateResponse(
        request,
        "contest/list.html",
        _base(request, user, groups=groups, mine=mine, only_mine=only_mine,
              total=len(items)),
    )


@router.post("")
async def contest_create(
    session: SessionDep,
    user: TeacherUser,
    title: str = Form(""),
    rules: str = Form("icpc"),
    team_size: int = Form(3),
    duration_min: int = Form(300),
    freeze_min: int = Form(0),
    planned_at: str = Form(""),
    description: str = Form(""),
):
    try:
        contest = await contests.create(
            session,
            user,
            title=title,
            rules=ContestRules(rules),
            team_size=team_size,
            duration_min=duration_min,
            freeze_min=freeze_min,
            planned_at=parse_local_input(planned_at),
            description=description,
        )
    except (ContestError, ValueError) as exc:
        return _go("/contest", error=str(exc))
    return _go(f"/contest/{contest.id}/edit", _("Черновик создан — добавьте задачи"))


# ------------------------------------------------------------------ обзор


@router.get("/{contest_id}")
async def contest_page(request: Request, session: SessionDep, user: CurrentUser, contest_id: int):
    contest = await _contest(session, user, contest_id)
    if contest is None:
        return _not_found()
    team = await contests.team_of(session, contest, user)
    board, my_row = None, None
    if contests.problems_open(contest, user):
        board = await contests.standings(session, contest, live=user.is_teacher)
        my_row = next((row for row in board.rows if team and row.team.id == team.id), None)
    return templates.TemplateResponse(
        request,
        "contest/show.html",
        _base(
            request,
            user,
            contest,
            team=team,
            board=board,
            my_row=my_row,
            problems_open=contests.problems_open(contest, user),
            team_count=len(await contests.teams(session, contest)),
        ),
    )


# ------------------------------------------------------------------ команда


@router.get("/{contest_id}/team")
async def team_page(request: Request, session: SessionDep, user: CurrentUser, contest_id: int):
    contest = await _contest(session, user, contest_id)
    if contest is None:
        return _not_found()
    team = await contests.team_of(session, contest, user)
    return templates.TemplateResponse(
        request,
        "contest/team.html",
        _base(
            request,
            user,
            contest,
            team=team,
            all_teams=await contests.teams(session, contest),
            # Код приходит ссылкой от капитана — подставляем его в поле.
            code=request.query_params.get("code", ""),
        ),
    )


@router.post("/{contest_id}/team")
async def team_create(
    session: SessionDep, user: CurrentUser, contest_id: int, name: str = Form("")
):
    contest = await _contest(session, user, contest_id)
    if contest is None:
        return _not_found()
    try:
        await contests.create_team(session, contest, user, name)
    except ContestError as exc:
        return _go(f"/contest/{contest_id}/team", error=str(exc))
    message = _("Команда создана — раздай код") if contest.is_team else _("Ты зарегистрирован")
    return _go(f"/contest/{contest_id}/team", message)


@router.post("/{contest_id}/team/join")
async def team_join(
    session: SessionDep, user: CurrentUser, contest_id: int, code: str = Form("")
):
    contest = await _contest(session, user, contest_id)
    if contest is None:
        return _not_found()
    try:
        team = await contests.join_team(session, contest, user, code)
    except ContestError as exc:
        return _go(f"/contest/{contest_id}/team?code={quote(code)}", error=str(exc))
    return _go(f"/contest/{contest_id}/team", _("Ты в команде «%(name)s»") % {"name": team.name})


@router.post("/{contest_id}/team/leave")
async def team_leave(session: SessionDep, user: CurrentUser, contest_id: int):
    contest = await _contest(session, user, contest_id)
    if contest is None:
        return _not_found()
    try:
        await contests.leave_team(session, contest, user)
    except ContestError as exc:
        return _go(f"/contest/{contest_id}/team", error=str(exc))
    return _go(f"/contest/{contest_id}/team", _("Регистрация отменена"))


# ------------------------------------------------------------------ задача


async def _problem_page(
    request: Request,
    session: SessionDep,
    user: User,
    contest: Contest,
    item,
    *,
    code: str | None = None,
    language: languages.Language | None = None,
    run: judge.RunResult | None = None,
    run_kind: str = "",
    error: str | None = None,
):
    """Страница задачи соревнования — общая для GET, прогона и сдачи, как
    у своей задачи: код из формы возвращается в редактор при любом исходе."""
    problem = item.problem
    task = await _task(session, problem.id)
    team = await contests.team_of(session, contest, user)
    history = await contests.runs(session, contest, team=team, user=user, problem_id=problem.id)
    choices = await languages.allowed(session)
    if language is None:
        language = languages.pick(choices, name=history[0].language if history else None)
    if code is not None:
        shown, shown_at = code, utcnow()
    elif history:
        shown, shown_at = history[0].code, history[0].submitted_at
    else:
        shown, shown_at = "", None
    open_tests = await tasks.tests_for(session, task, only_open=True) if task else []
    return templates.TemplateResponse(
        request,
        "contest/problem.html",
        _base(
            request,
            user,
            contest,
            item=item,
            problem=problem,
            task=task,
            team=team,
            statement=notebook.render_markdown(task.statement) if task else "",
            open_tests=open_tests,
            open_by_position={test.position: test for test in open_tests},
            history=history,
            judge_ready=(await judge.config(session)).ready,
            judge_down=not (await judge.health(session)).ok,
            code=shown,
            code_at=_epoch_ms(shown_at),
            code_languages=choices,
            code_language=language,
            run=run,
            run_kind=run_kind,
            # Сдавать в табло может только участник идущего тура; остальное
            # — дорешивание, и об этом честно написано у кнопки.
            counts=contest.phase() == "running" and team is not None and not user.is_teacher,
            error=error if error is not None else request.query_params.get("err"),
        ),
    )


async def _task(session: SessionDep, problem_id: int) -> Task | None:
    return await session.scalar(select(Task).where(Task.problem_id == problem_id))


async def _open_problem(session: SessionDep, user: User, contest_id: int, letter: str):
    contest = await _contest(session, user, contest_id)
    if contest is None or not contests.problems_open(contest, user):
        return None, None
    return contest, contests.problem_by_letter(contest, letter)


@router.get("/{contest_id}/problems/{letter}")
async def problem_page(
    request: Request, session: SessionDep, user: CurrentUser, contest_id: int, letter: str
):
    contest, item = await _open_problem(session, user, contest_id, letter)
    if contest is None:
        return _not_found()
    if item is None:
        return _go(f"/contest/{contest_id}", error=_("Задача не найдена"))
    return await _problem_page(request, session, user, contest, item)


@router.post("/{contest_id}/problems/{letter}/run")
async def problem_run(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    contest_id: int,
    letter: str,
    code: str = Form(""),
    language_id: Annotated[OptionalInt, Form()] = None,
):
    """Прогон по открытым тестам. В табло не идёт и штрафа не даёт."""
    contest, item = await _open_problem(session, user, contest_id, letter)
    if contest is None or item is None:
        return _not_found()
    language = languages.pick(await languages.allowed(session), language_id)
    page = {"code": code, "language": language}
    text, error = _solution(code)
    if error:
        return await _problem_page(request, session, user, contest, item, **page, error=error)
    task = await _task(session, item.problem_id)
    cfg = await judge.config(session)
    open_tests = await tasks.test_refs(session, task, only_open=True)
    team = await contests.team_of(session, contest, user)
    try:
        async with _one_at_a_time(("contest-team", team.id) if team else user.id):
            result = await _judged(
                session, cfg, text, open_tests, (task.time_limit_ms, task.memory_limit_mb),
                language.id,
            )
    except _Busy:
        return await _problem_page(
            request, session, user, contest, item, **page,
            error=_("У команды уже идёт проверка — подождите её конца"),
        )
    except judge.JudgeUnavailable as exc:
        await judge.note(session, str(exc))
        return await _problem_page(
            request, session, user, contest, item, **page,
            error=_("Проверка недоступна: %(why)s") % {"why": exc},
        )
    await judge.note(session)
    return await _problem_page(
        request, session, user, contest, item, code=text, language=language,
        run=result, run_kind="run",
    )


@router.post("/{contest_id}/problems/{letter}/submit")
async def problem_submit(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    contest_id: int,
    letter: str,
    code: str = Form(""),
    language_id: Annotated[OptionalInt, Form()] = None,
):
    """Посылка: все тесты. Во время тура — в табло команды, иначе дорешивание."""
    contest, item = await _open_problem(session, user, contest_id, letter)
    if contest is None or item is None:
        return _not_found()
    language = languages.pick(await languages.allowed(session), language_id)
    page = {"code": code, "language": language}
    text, error = _solution(code)
    if error:
        return await _problem_page(request, session, user, contest, item, **page, error=error)

    team = await contests.team_of(session, contest, user)
    if contest.phase() == "running" and team is None and not user.is_teacher:
        return await _problem_page(
            request, session, user, contest, item, **page,
            error=_("Сначала зарегистрируйся — посылки без команды в табло не идут"),
        )

    task = await _task(session, item.problem_id)
    result = None
    cfg = await judge.config(session)
    if cfg.ready:
        all_tests = await tasks.test_refs(session, task)
        try:
            async with _one_at_a_time(("contest-team", team.id) if team else user.id):
                result = await _judged(
                    session, cfg, text, all_tests, (task.time_limit_ms, task.memory_limit_mb),
                    language.id,
                )
        except _Busy:
            return await _problem_page(
                request, session, user, contest, item, **page,
                error=_("У команды уже идёт проверка — подождите её конца"),
            )
        except judge.JudgeUnavailable as exc:
            # Посылку не записываем: команда не виновата, что судья лёг,
            # и штрафа за это быть не должно.
            await judge.note(session, str(exc))
            return await _problem_page(
                request, session, user, contest, item, **page,
                error=_("Проверка недоступна: %(why)s") % {"why": exc},
            )
        await judge.note(session)

    # Пока судья думал, тур мог закончиться: тогда посылка — дорешивание.
    # Решает `record_run` по текущему времени, а не по моменту нажатия.
    await session.refresh(contest)
    await contests.record_run(
        session, contest, user=user, team=team, problem=item.problem,
        code=text, language=language.name, result=result,
    )
    return await _problem_page(
        request, session, user, contest, item, code=text, language=language,
        run=result, run_kind="submit",
    )


# ------------------------------------------------------------------ табло и посылки


@router.get("/{contest_id}/standings")
async def standings_page(
    request: Request, session: SessionDep, user: CurrentUser, contest_id: int
):
    contest = await _contest(session, user, contest_id)
    if contest is None:
        return _not_found()
    board = None
    if contests.problems_open(contest, user):
        board = await contests.standings(session, contest, live=user.is_teacher)
    team = await contests.team_of(session, contest, user)
    return templates.TemplateResponse(
        request,
        "contest/standings.html",
        _base(request, user, contest, board=board, team=team,
              icpc=contest.rules == ContestRules.icpc),
    )


@router.get("/{contest_id}/runs")
async def runs_page(request: Request, session: SessionDep, user: CurrentUser, contest_id: int):
    contest = await _contest(session, user, contest_id)
    if contest is None:
        return _not_found()
    team = await contests.team_of(session, contest, user)
    if user.is_teacher:
        items = await contests.runs(session, contest)
    else:
        items = await contests.runs(session, contest, team=team, user=user)
    teams = {t.id: t for t in await contests.teams(session, contest)}
    letters = {item.problem_id: item.letter for item in contest.problems}
    return templates.TemplateResponse(
        request,
        "contest/runs.html",
        _base(request, user, contest, runs=items, teams=teams, letters=letters, team=team),
    )


@router.get("/{contest_id}/runs/{run_id}")
async def run_page(
    request: Request, session: SessionDep, user: CurrentUser, contest_id: int, run_id: int
):
    contest = await _contest(session, user, contest_id)
    run = await session.get(ContestRun, run_id)
    if contest is None or run is None or run.contest_id != contest.id:
        return _not_found()
    if not await contests.may_read_run(session, contest, run, user):
        return _go(f"/contest/{contest_id}/runs", error=_("Это посылка другой команды"))
    team = await session.get(ContestTeam, run.team_id) if run.team_id else None
    letters = {item.problem_id: item.letter for item in contest.problems}
    return templates.TemplateResponse(
        request,
        "contest/run.html",
        _base(request, user, contest, run=run, run_team=team, letter=letters.get(run.problem_id)),
    )


# ------------------------------------------------------------------ преподаватель


@router.get("/{contest_id}/edit")
async def edit_page(request: Request, session: SessionDep, user: TeacherUser, contest_id: int):
    contest = await session.get(Contest, contest_id)
    if contest is None:
        return _not_found()
    sets = (
        await session.execute(select(ProblemSet).order_by(ProblemSet.id.desc()))
    ).scalars().all()
    return templates.TemplateResponse(
        request,
        "contest/edit.html",
        _base(
            request,
            user,
            contest,
            rules=list(ContestRules),
            own=await contests.own_tasks(session),
            sets=sets,
            all_teams=await contests.teams(session, contest),
            slugs="\n".join(item.problem.slug for item in contest.problems),
        ),
    )


async def _teacher_contest(session: SessionDep, contest_id: int) -> Contest | None:
    return await session.get(Contest, contest_id)


@router.post("/{contest_id}/edit")
async def edit_save(
    session: SessionDep,
    user: TeacherUser,
    contest_id: int,
    title: str = Form(""),
    rules: str = Form("icpc"),
    team_size: int = Form(3),
    duration_min: int = Form(300),
    freeze_min: int = Form(0),
    planned_at: str = Form(""),
    description: str = Form(""),
):
    contest = await _teacher_contest(session, contest_id)
    if contest is None:
        return _not_found()
    back = f"/contest/{contest_id}/edit"
    try:
        await contests.update(
            session,
            contest,
            title=title,
            rules=ContestRules(rules),
            team_size=team_size,
            duration_min=duration_min,
            freeze_min=freeze_min,
            planned_at=parse_local_input(planned_at),
            description=description,
        )
    except (ContestError, ValueError) as exc:
        return _go(back, error=str(exc))
    return _go(back, _("Настройки сохранены"))


@router.post("/{contest_id}/problems")
async def problems_save(
    session: SessionDep,
    user: TeacherUser,
    contest_id: int,
    slugs: str = Form(""),
    problem_set_id: Annotated[OptionalInt, Form()] = None,
):
    """Задачи списком слагов или целиком из списка задач."""
    contest = await _teacher_contest(session, contest_id)
    if contest is None:
        return _not_found()
    back = f"/contest/{contest_id}/edit"
    wanted = (
        await contests.slugs_of_set(session, problem_set_id)
        if problem_set_id
        else slugs.replace(",", "\n").splitlines()
    )
    try:
        chosen = await contests.set_problems(session, contest, wanted)
    except ContestError as exc:
        return _go(back, error=str(exc))
    return _go(back, _("Задач в соревновании: %(count)s") % {"count": len(chosen)})


@router.post("/{contest_id}/publish")
async def publish(
    session: SessionDep, user: TeacherUser, contest_id: int, on: str = Form("true")
):
    contest = await _teacher_contest(session, contest_id)
    if contest is None:
        return _not_found()
    back = f"/contest/{contest_id}/edit"
    try:
        await contests.publish(session, contest, on == "true")
    except ContestError as exc:
        return _go(back, error=str(exc))
    return _go(back, _("Регистрация открыта") if on == "true" else _("Соревнование скрыто"))


@router.post("/{contest_id}/start")
async def start(session: SessionDep, user: TeacherUser, contest_id: int):
    contest = await _teacher_contest(session, contest_id)
    if contest is None:
        return _not_found()
    try:
        await contests.start(session, contest)
    except ContestError as exc:
        return _go(f"/contest/{contest_id}/edit", error=str(exc))
    return _go(f"/contest/{contest_id}/standings", _("Старт! Задачи открыты"))


@router.post("/{contest_id}/extend")
async def extend(
    session: SessionDep, user: TeacherUser, contest_id: int, minutes: int = Form(15)
):
    contest = await _teacher_contest(session, contest_id)
    if contest is None:
        return _not_found()
    back = f"/contest/{contest_id}/edit"
    try:
        await contests.extend(session, contest, minutes)
    except ContestError as exc:
        return _go(back, error=str(exc))
    return _go(back, _("Добавлено минут: %(count)s") % {"count": minutes})


@router.post("/{contest_id}/finish")
async def finish(session: SessionDep, user: TeacherUser, contest_id: int):
    contest = await _teacher_contest(session, contest_id)
    if contest is None:
        return _not_found()
    try:
        await contests.finish(session, contest)
    except ContestError as exc:
        return _go(f"/contest/{contest_id}/edit", error=str(exc))
    return _go(f"/contest/{contest_id}/standings", _("Соревнование завершено, табло разморожено"))


@router.post("/{contest_id}/teams/{team_id}/delete")
async def team_delete(session: SessionDep, user: TeacherUser, contest_id: int, team_id: int):
    team = await session.get(ContestTeam, team_id)
    if team is None or team.contest_id != contest_id:
        return _go(f"/contest/{contest_id}/edit", error=_("Команда не найдена"))
    await contests.delete_team(session, team)
    return _go(f"/contest/{contest_id}/edit#teams", _("Команда удалена"))


@router.post("/{contest_id}/teams/{team_id}/members/{user_id}/delete")
async def member_delete(
    session: SessionDep, user: TeacherUser, contest_id: int, team_id: int, user_id: int
):
    team = await session.get(ContestTeam, team_id)
    if team is None or team.contest_id != contest_id:
        return _go(f"/contest/{contest_id}/edit", error=_("Команда не найдена"))
    await contests.remove_member(session, team, user_id)
    return _go(f"/contest/{contest_id}/edit#teams", _("Участник убран из команды"))
