"""Инструменты портала для нейросетей.

Каждый — тонкая обёртка над сервисами, которыми пользуются и страницы:
доступ к заданию — `may_see_assignment`, зачёт — `Cell.counts`, создание
списка — `problem_sets.create`. Своих правил здесь нет; если для инструмента
не нашлось сервиса, сначала выносится сервис, и страница переходит на него же.

Докстрока функции — это описание, которое читает модель. Пишем его как
подсказку коллеге: что вернёт, когда звать, чем заканчивается вызов.

Студенту — только своё и только чтение: свои задания, задачи из них, свой
зачёт, объявления. Ни чужих решений, ни закрытых тестов, ни правок.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, func, select

from app.config import settings
from app.i18n import mark as N_
from app.mcp.registry import STUDENT, STUDENT_ONLY, TEACHER, Context, ToolError, tool
from app.models import (
    Assignment,
    Group,
    GroupMembership,
    Platform,
    Problem,
    ProblemSet,
    ProblemSetItem,
    SheetKind,
    SheetScale,
    Task,
    TaskTest,
    User,
    utcnow,
)
from app.notify import portal_url
from app.routers.announcements import list_visible
from app.services import assignments as assignments_service
from app.services import messages, problem_sets, sheet, similarity, solutions
from app.services import tasks as tasks_service
from app.services.problem_parser import parse_problem_list, search_problems
from app.services.progress import (
    assignments_for_user,
    compute_progress,
    groups_for_user,
    may_see_assignment,
    participants_for_assignment,
    problems_for_set,
)
from app.templating import LOCAL_TZ

# Сколько кода решений отдаём за один вызов. Контрольная на 30 человек и
# 5 задач — это 150 решений; целиком они не влезут в окно модели, и ответ
# оборвётся посередине. Лучше честно сказать «сузь выборку».
MAX_CODE_CHARS = 200_000


class Params(BaseModel):
    # Лишний параметр — почти всегда опечатка модели. Пусть узнает о ней сразу.
    model_config = ConfigDict(extra="forbid")


class NoParams(Params):
    pass


# ---------------------------------------------------------------- вывод


def _when(value: datetime | None) -> str | None:
    """Время в поясе портала: преподаватель думает «до 23:59 по Москве», а не в UTC."""
    if value is None:
        return None
    return value.astimezone(LOCAL_TZ).isoformat(timespec="minutes")


def _moment(value: datetime | None) -> datetime | None:
    """Время от модели. Без пояса — местное время портала, как в форме на сайте."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=LOCAL_TZ)
    return value.astimezone(UTC)


def _ref(problem: Problem) -> str:
    """Запись задачи, которую понимает разбор списка: её можно отдать обратно."""
    if problem.platform == Platform.codeforces:
        return f"cf:{problem.external_id}"
    if problem.platform == Platform.local:
        return f"task:{problem.slug}"
    return f"lc:{problem.slug}"


def _problem(problem: Problem) -> dict:
    return {
        "ref": _ref(problem),
        "platform": problem.platform.value,
        "title": problem.title,
        "url": portal_url(problem.url) if problem.url.startswith("/") else problem.url,
        "difficulty": problem.difficulty,
        "rating": problem.rating,
        "tags": problem.tags or [],
        "paid_only": problem.is_paid_only,
    }


async def _group_titles(ctx: Context, group_ids) -> dict[int, str]:
    """Названия групп одним запросом.

    Не через `assignment.group`: у только что выданного задания связь не
    загружена, а ленивая подгрузка в асинхронной сессии падает.
    """
    ids = {group_id for group_id in group_ids if group_id is not None}
    if not ids:
        return {}
    rows = await ctx.session.execute(select(Group.id, Group.title).where(Group.id.in_(ids)))
    return dict(rows.all())


def _target(group_id: int | None, user_id: int | None, groups: dict[int, str]) -> str:
    if group_id is not None:
        return groups.get(group_id, "")
    return "лично" if user_id is not None else "все студенты"


def _phase(assignment: Assignment) -> str:
    now = utcnow()
    if assignment.assigned_at > now:
        return "not_started"
    if assignment.deadline is not None and assignment.deadline < now:
        return "closed"
    return "open"


def _assignment(assignment: Assignment, groups: dict[int, str], *, teacher: bool) -> dict:
    path = f"/teacher/assignments/{assignment.id}" if teacher else f"/assignments/{assignment.id}"
    return {
        "id": assignment.id,
        "title": assignment.title,
        "description": assignment.description,
        "for": _target(assignment.group_id, assignment.user_id, groups),
        "group_id": assignment.group_id,
        "problem_set_id": assignment.problem_set_id,
        "starts_at": _when(assignment.assigned_at),
        "deadline": _when(assignment.deadline),
        "phase": _phase(assignment),
        "requires_solution_code": assignment.requires_solution,
        "url": portal_url(path),
    }


async def _describe(ctx: Context, assignments, *, teacher: bool) -> list[dict]:
    assignments = list(assignments)
    groups = await _group_titles(ctx, (a.group_id for a in assignments))
    return [_assignment(a, groups, teacher=teacher) for a in assignments]


async def _resolve(ctx: Context, raw: str) -> Problem:
    found = await parse_problem_list(ctx.session, raw.strip())
    if not found.problems:
        raise ToolError(
            f"Задачи {raw!r} нет в каталоге портала. Пиши ссылку, слаг LeetCode, "
            "код Codeforces вроде 4A или task:слаг своей задачи."
        )
    return found.problems[0]


async def _assignment_for(ctx: Context, assignment_id: int) -> Assignment:
    assignment = await ctx.session.get(Assignment, assignment_id)
    # Чужое задание для студента неотличимо от несуществующего.
    if assignment is None or not await may_see_assignment(ctx.session, ctx.user, assignment):
        raise ToolError(f"Задание {assignment_id} не найдено")
    return assignment


async def _my_problem_ids(ctx: Context) -> set[int]:
    """Задачи из заданий, которые студенту видны, — тот же круг, что на главной."""
    set_ids = {a.problem_set_id for a in await assignments_for_user(ctx.session, ctx.user)}
    if not set_ids:
        return set()
    rows = await ctx.session.execute(
        select(ProblemSetItem.problem_id).where(ProblemSetItem.problem_set_id.in_(set_ids))
    )
    return set(rows.scalars())


async def _usage(ctx: Context, entries) -> list[dict]:
    groups = await _group_titles(
        ctx, (a.group_id for _, assignments in entries for a in assignments)
    )
    return [
        {
            "problem_set_id": problem_set.id,
            "problem_set": problem_set.title,
            "assignments": [
                {
                    "id": assignment.id,
                    "title": assignment.title,
                    "for": _target(assignment.group_id, assignment.user_id, groups),
                    "starts_at": _when(assignment.assigned_at),
                }
                for assignment in assignments
            ],
        }
        for problem_set, assignments in entries
    ]


# ---------------------------------------------------------------- всем


@tool(
    "whoami",
    title=N_("Кто я"),
    hint=N_("имя, роль и группы"),
    roles=STUDENT,
)
async def whoami(ctx: Context, args: NoParams) -> dict:
    """Кто подключён к порталу: имя, роль, группы и разрешает ли ключ изменения.

    Позови первым, если не знаешь, с кем работаешь: от роли зависит, какие
    инструменты есть. Время на портале — в поясе, указанном в `timezone`.
    """
    groups = await groups_for_user(ctx.session, ctx.user)
    return {
        "id": ctx.user.id,
        "name": ctx.user.display_name,
        "role": ctx.user.role.value,
        "groups": [{"id": group.id, "title": group.title} for group in groups],
        "can_write": ctx.can_write,
        "portal": settings.base_url,
        "timezone": settings.display_timezone,
    }


class AssignmentsParams(Params):
    group_id: int | None = Field(
        None, description="Только задания этой группы (для преподавателя)."
    )


@tool(
    "list_assignments",
    title=N_("Задания"),
    hint=N_("задания, сроки и сколько засчитано"),
    roles=STUDENT,
)
async def list_assignments(ctx: Context, args: AssignmentsParams) -> dict:
    """Задания: название, кому, начало, дедлайн, фаза (not_started / open / closed).

    Студенту — свои задания и сколько задач уже засчитано. Преподавателю —
    все задания портала, можно сузить до группы. Подробности одного задания —
    `get_assignment`.
    """
    if not ctx.user.is_teacher:
        mine = await assignments_for_user(ctx.session, ctx.user)
        items = await _describe(ctx, mine, teacher=False)
        for item, assignment in zip(items, mine, strict=True):
            progress = await compute_progress(ctx.session, assignment, [ctx.user])
            item.update(
                solved=progress.solved_count(ctx.user.id), total=progress.total_problems
            )
        return {"assignments": items}

    stmt = select(Assignment).order_by(Assignment.assigned_at.desc())
    if args.group_id is not None:
        stmt = stmt.where(Assignment.group_id == args.group_id)
    found = list((await ctx.session.execute(stmt)).scalars())
    sizes = dict(
        (
            await ctx.session.execute(
                select(ProblemSetItem.problem_set_id, func.count()).group_by(
                    ProblemSetItem.problem_set_id
                )
            )
        ).all()
    )
    items = await _describe(ctx, found, teacher=True)
    for item, assignment in zip(items, found, strict=True):
        item["problems"] = sizes.get(assignment.problem_set_id, 0)
    return {"assignments": items}


class AssignmentParams(Params):
    assignment_id: int = Field(description="Номер задания из `list_assignments`.")


@tool(
    "get_assignment",
    title=N_("Задание целиком"),
    hint=N_("задачи задания и статус каждой"),
    roles=STUDENT,
)
async def get_assignment(ctx: Context, args: AssignmentParams) -> dict:
    """Одно задание: условия выдачи, задачи со ссылками и последние сообщения
    преподавателя к нему (`messages`, свежие первыми: ссылки на комнаты
    для совместного решения, объявления по ходу пары).

    Студенту — по каждой задаче его статус: solved_in_time (засчитано, если
    `counts`), solved_late (после дедлайна, не засчитывается), solved_before
    (решена до выдачи, не засчитывается), not_solved; и нужен ли ещё код.
    Преподавателю — сколько участников закрыло каждую задачу; по людям —
    `get_assignment_progress`.
    """
    assignment = await _assignment_for(ctx, args.assignment_id)
    teacher = ctx.user.is_teacher
    out = (await _describe(ctx, [assignment], teacher=teacher))[0]
    out["messages"] = [
        _message(item)
        for item in await messages.for_assignment(ctx.session, assignment.id, limit=10)
    ]

    if not teacher:
        progress = await compute_progress(ctx.session, assignment, [ctx.user])
        problems = []
        for problem in progress.problems:
            cell = progress.cell(ctx.user.id, problem.id)
            problems.append(
                {
                    **_problem(problem),
                    "status": cell.status.value,
                    "counts": cell.counts,
                    "code_review": cell.review.value if cell.review else None,
                    "code_missing": cell.code_missing,
                }
            )
        out.update(
            solved=progress.solved_count(ctx.user.id),
            total=progress.total_problems,
            problems=problems,
        )
        return out

    participants = [
        u for u in await participants_for_assignment(ctx.session, assignment) if not u.is_teacher
    ]
    progress = await compute_progress(ctx.session, assignment, participants)
    out.update(
        participants=len(participants),
        problems=[
            {**_problem(problem), "solved_by": progress.problem_solved_count(problem.id)}
            for problem in progress.problems
        ],
    )
    return out


class ProblemParams(Params):
    problem: str = Field(
        description="Ссылка на задачу, слаг LeetCode (two-sum), код Codeforces (cf:4A) "
        "или своя задача (task:слаг)."
    )


@tool(
    "get_problem",
    title=N_("Условие задачи"),
    hint=N_("условие, лимиты и открытые тесты"),
    roles=STUDENT,
)
async def get_problem(ctx: Context, args: ProblemParams) -> dict:
    """Задача: площадка, сложность, метки, ссылка. У своих задач портала — ещё
    условие в markdown, лимиты и открытые тесты.

    Закрытые тесты не отдаются никому — только их число. Студенту доступны
    задачи из его заданий; преподаватель видит ещё, в каких списках и
    заданиях задача уже стояла.
    """
    problem = await _resolve(ctx, args.problem)
    if not ctx.user.is_teacher and problem.id not in await _my_problem_ids(ctx):
        raise ToolError("Этой задачи нет в твоих заданиях")

    out = _problem(problem)
    if problem.platform == Platform.local:
        task = await ctx.session.scalar(select(Task).where(Task.problem_id == problem.id))
        if task is not None:
            tests = await tasks_service.tests_for(ctx.session, task)
            out.update(
                statement=task.statement,
                time_limit_ms=task.time_limit_ms,
                memory_limit_mb=task.memory_limit_mb,
                # Закрытые тесты не покидают портал: ни ввода, ни ответа.
                open_tests=[
                    {"input": test.stdin, "output": test.expected}
                    for test in tests
                    if test.is_open
                ],
                closed_tests=sum(1 for test in tests if not test.is_open),
            )
    if ctx.user.is_teacher:
        usage = await problem_sets.used_in(ctx.session, [problem.id])
        out["used_in"] = await _usage(ctx, usage.get(problem.id, []))
    return out


class AnnouncementsParams(Params):
    include_past: bool = Field(False, description="Показать и прошедшие события.")


@tool(
    "list_announcements",
    title=N_("Анонсы"),
    hint=N_("анонсы контестов и новости"),
    roles=STUDENT,
)
async def list_announcements(ctx: Context, args: AnnouncementsParams) -> dict:
    """Объявления: контесты, сборы, новости — то же, что в разделе «Анонсы».

    `state`: upcoming — ещё не началось, live — идёт, past — прошло,
    plain — без даты.
    """
    now = utcnow()
    visible = await list_visible(ctx.session, ctx.user)
    groups = await _group_titles(ctx, (item.group_id for item in visible))
    items = []
    for item in visible:
        state = item.state(now)
        if state == "past" and not args.include_past:
            continue
        items.append(
            {
                "id": item.id,
                "title": item.title,
                "body": item.body,
                "url": item.url,
                "starts_at": _when(item.starts_at),
                "ends_at": _when(item.ends_at),
                "state": state,
                "for": groups.get(item.group_id, "") if item.group_id else "все",
                "pinned": item.pinned,
            }
        )
    return {"announcements": items}


# ---------------------------------------------------------------- преподавателю: задачи


class SearchParams(Params):
    query: str = Field("", description="Часть названия, слага или кода задачи.")
    platform: Literal["leetcode", "codeforces", "local"] | None = None
    tag: str | None = Field(
        None,
        description="Метка целиком, как на площадке: «binary search», «dp», "
        "«Two Pointers». Регистр не важен.",
    )
    difficulty: Literal["Easy", "Medium", "Hard"] | None = Field(
        None, description="Сложность LeetCode."
    )
    min_rating: int | None = Field(None, description="Рейтинг Codeforces от.")
    max_rating: int | None = Field(None, description="Рейтинг Codeforces до.")
    limit: int = Field(30, ge=1, le=100)


@tool(
    "search_problems",
    title=N_("Поиск по каталогу задач"),
    hint=N_("по меткам, сложности и рейтингу"),
    roles=TEACHER,
)
async def search_catalog(ctx: Context, args: SearchParams) -> dict:
    """Ищет в каталоге портала (LeetCode, Codeforces и свои задачи) по названию,
    метке, сложности и рейтингу.

    В список задания попадает только то, что есть в каталоге, поэтому
    подбирая задачи, проверяй их здесь или через `preview_problem_list`.
    `used_in` — в каких списках задача уже стоит: повтор того, что группа
    уже решала, на контрольной ничего не проверит. Платные задачи LeetCode
    помечены `paid_only` — у студентов без подписки они не откроются.
    """
    found = await search_problems(
        ctx.session,
        args.query,
        Platform(args.platform) if args.platform else None,
        args.limit,
        tag=args.tag,
        difficulty=args.difficulty,
        min_rating=args.min_rating,
        max_rating=args.max_rating,
    )
    usage = await problem_sets.used_in(ctx.session, [p.id for p in found])
    return {
        "problems": [
            {
                **_problem(problem),
                "used_in": [entry[0].title for entry in usage.get(problem.id, [])],
            }
            for problem in found
        ]
    }


class ProblemListParams(Params):
    problems: list[str] = Field(
        min_length=1,
        max_length=100,
        description="По задаче на элемент: ссылка LeetCode или Codeforces, слаг "
        "LeetCode, cf:1352G, task:слаг.",
    )


@tool(
    "preview_problem_list",
    title=N_("Проверить список задач"),
    hint=N_("что не распознано и что группа уже решала"),
    roles=TEACHER,
)
async def preview_problem_list(ctx: Context, args: ProblemListParams) -> dict:
    """Пробный разбор списка задач — ничего не создаёт.

    Покажет, какие строки портал узнал, какие нет, где задачи уже стояли
    (списки и задания, когда выдавались) и есть ли повторы. Зови перед
    `create_problem_set`, а итог покажи человеку: нераспознанное не попадёт
    в список, а повтор старой задачи он, возможно, не заметил.
    """
    resolved: list[tuple[str, Problem]] = []
    unresolved: list[str] = []
    for line in args.problems:
        found = await parse_problem_list(ctx.session, line)
        if found.problems:
            resolved.append((line, found.problems[0]))
        elif line.strip():
            unresolved.append(line)

    usage = await problem_sets.used_in(ctx.session, [problem.id for _, problem in resolved])
    seen: set[int] = set()
    items = []
    for line, problem in resolved:
        items.append(
            {
                "input": line,
                **_problem(problem),
                "duplicate": problem.id in seen,
                "used_in": await _usage(ctx, usage.get(problem.id, [])),
            }
        )
        seen.add(problem.id)
    out: dict = {"resolved": items, "unresolved": unresolved}
    if unresolved:
        out["hint"] = (
            "Нераспознанных задач нет в каталоге. Проверь слаг или код; новые задачи "
            "площадок появляются после обновления каталога (кнопка на панели "
            "преподавателя, раз в сутки — сама)."
        )
    return out


@tool(
    "list_own_tasks",
    title=N_("Свои задачи портала"),
    hint=N_("лимиты и число тестов"),
    roles=TEACHER,
)
async def list_own_tasks(ctx: Context, args: NoParams) -> dict:
    """Свои задачи портала (с проверкой судьёй): ссылка task:слаг, лимиты, число
    открытых и закрытых тестов. Загружаются архивом на странице «Свои задачи»."""
    test_counts = select(
        TaskTest.task_id,
        func.sum(case((TaskTest.is_open, 1), else_=0)).label("open"),
        func.count().label("total"),
    ).group_by(TaskTest.task_id)
    counts = {row.task_id: row for row in (await ctx.session.execute(test_counts)).all()}
    rows = await ctx.session.execute(
        select(Task, Problem).join(Problem, Problem.id == Task.problem_id).order_by(Task.created_at)
    )
    items = []
    for task, problem in rows.all():
        row = counts.get(task.id)
        open_tests = int(row.open or 0) if row else 0
        items.append(
            {
                **_problem(problem),
                "time_limit_ms": task.time_limit_ms,
                "memory_limit_mb": task.memory_limit_mb,
                "open_tests": open_tests,
                "closed_tests": (row.total - open_tests) if row else 0,
            }
        )
    return {"tasks": items}


# ---------------------------------------------------------------- преподавателю: списки


class SetsParams(Params):
    query: str = Field("", description="Часть названия списка.")


@tool(
    "list_problem_sets",
    title=N_("Списки задач"),
    hint=N_("списки и где они выданы"),
    roles=TEACHER,
)
async def list_problem_sets(ctx: Context, args: SetsParams) -> dict:
    """Списки задач портала: сколько задач и в скольких заданиях выдан.

    Посмотри перед созданием нового — может, нужный уже есть.
    """
    stmt = select(ProblemSet).order_by(ProblemSet.created_at.desc())
    if args.query.strip():
        stmt = stmt.where(func.lower(ProblemSet.title).like(f"%{args.query.strip().lower()}%"))
    sets = list((await ctx.session.execute(stmt)).scalars())
    issued = dict(
        (
            await ctx.session.execute(
                select(Assignment.problem_set_id, func.count()).group_by(Assignment.problem_set_id)
            )
        ).all()
    )
    sizes = dict(
        (
            await ctx.session.execute(
                select(ProblemSetItem.problem_set_id, func.count()).group_by(
                    ProblemSetItem.problem_set_id
                )
            )
        ).all()
    )
    return {
        "problem_sets": [
            {
                "id": problem_set.id,
                "title": problem_set.title,
                "description": problem_set.description,
                "problems": sizes.get(problem_set.id, 0),
                "assignments": issued.get(problem_set.id, 0),
                "created_at": _when(problem_set.created_at),
            }
            for problem_set in sets
        ]
    }


class SetParams(Params):
    problem_set_id: int


@tool(
    "get_problem_set",
    title=N_("Список задач целиком"),
    hint=N_("задачи списка и задания по нему"),
    roles=TEACHER,
)
async def get_problem_set(ctx: Context, args: SetParams) -> dict:
    """Один список: задачи по порядку и задания, в которых он выдан."""
    problem_set = await ctx.session.get(ProblemSet, args.problem_set_id)
    if problem_set is None:
        raise ToolError(f"Списка {args.problem_set_id} нет")
    issued = (
        await ctx.session.execute(
            select(Assignment).where(Assignment.problem_set_id == problem_set.id)
        )
    ).scalars()
    return {
        "id": problem_set.id,
        "title": problem_set.title,
        "description": problem_set.description,
        "url": portal_url(f"/teacher/sets/{problem_set.id}"),
        "problems": [
            _problem(problem) for problem in await problems_for_set(ctx.session, problem_set.id)
        ],
        "assignments": await _describe(ctx, issued, teacher=True),
    }


class CreateSetParams(Params):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field("", max_length=2000)
    problems: list[str] = Field(
        min_length=1,
        max_length=100,
        description="Задачи по порядку, в тех же записях, что у `preview_problem_list`.",
    )


@tool(
    "create_problem_set",
    title=N_("Создать список задач"),
    hint=N_("студенты не видят его до выдачи"),
    roles=TEACHER,
    writes=True,
)
async def create_problem_set(ctx: Context, args: CreateSetParams) -> dict:
    """Создаёт список задач. Студенты его не видят, пока по нему не выдано задание.

    Сначала проверь задачи через `preview_problem_list` и согласуй список с
    человеком. Нераспознанные строки в список не попадут — они вернутся в
    `unresolved`.
    """
    if not args.title.strip():
        raise ToolError("Пустое название")
    problem_set, result = await problem_sets.create(
        ctx.session, ctx.user, args.title, args.description, "\n".join(args.problems)
    )
    return {
        "id": problem_set.id,
        "url": portal_url(f"/teacher/sets/{problem_set.id}"),
        "added": [_ref(problem) for problem in result.problems],
        "unresolved": result.unresolved,
    }


class AddToSetParams(Params):
    problem_set_id: int
    problems: list[str] = Field(min_length=1, max_length=100)


@tool(
    "add_problems_to_set",
    title=N_("Добавить задачи в список"),
    hint=N_("дописывает в конец списка"),
    roles=TEACHER,
    writes=True,
)
async def add_problems_to_set(ctx: Context, args: AddToSetParams) -> dict:
    """Дописывает задачи в конец списка; уже стоящие пропускаются.

    Если список уже выдан, новые задачи сразу появятся у студентов в этих
    заданиях — скажи об этом человеку до вызова.
    """
    problem_set = await ctx.session.get(ProblemSet, args.problem_set_id)
    if problem_set is None:
        raise ToolError(f"Списка {args.problem_set_id} нет")
    added, unresolved = await problem_sets.add(ctx.session, problem_set, "\n".join(args.problems))
    return {"added": [_ref(problem) for problem in added], "unresolved": unresolved}


class RemoveFromSetParams(Params):
    problem_set_id: int
    problems: list[str] = Field(min_length=1, max_length=100)


@tool(
    "remove_problems_from_set",
    title=N_("Убрать задачи из списка"),
    hint=N_("и из всех заданий по нему; посылки студентов остаются"),
    roles=TEACHER,
    writes=True,
)
async def remove_problems_from_set(ctx: Context, args: RemoveFromSetParams) -> dict:
    """Убирает задачи из списка — и тем самым из всех заданий, выданных по
    нему, в том числе уже начавшихся. Как кнопка «убрать» в таблице задач
    на странице задания.

    Посылки студентов остаются, зачёт пересчитывается сам. Перед вызовом
    скажи человеку, из каких заданий пропадут задачи (`get_problem_set`
    показывает, где список выдан), и дождись явного «да».
    """
    problem_set = await ctx.session.get(ProblemSet, args.problem_set_id)
    if problem_set is None:
        raise ToolError(f"Списка {args.problem_set_id} нет")
    wanted = [await _resolve(ctx, raw) for raw in args.problems]
    in_set = {item.problem_id for item in problem_set.items}
    missing = [_ref(p) for p in wanted if p.id not in in_set]
    removed = await problem_sets.remove(
        ctx.session, problem_set.id, [p.id for p in wanted if p.id in in_set]
    )
    affected = (
        await ctx.session.execute(
            select(Assignment).where(Assignment.problem_set_id == problem_set.id)
        )
    ).scalars().all()
    return {
        "removed": removed,
        "not_in_set": missing,
        "assignments": await _describe(ctx, list(affected), teacher=True),
    }


# ---------------------------------------------------------------- преподавателю: группы и задания


class GroupsParams(Params):
    include_archived: bool = False


@tool(
    "list_groups",
    title=N_("Группы"),
    hint=N_("размер и коды вступления"),
    roles=TEACHER,
)
async def list_groups(ctx: Context, args: GroupsParams) -> dict:
    """Группы: номер, название, сколько человек, код вступления."""
    stmt = select(Group).order_by(Group.title)
    if not args.include_archived:
        stmt = stmt.where(Group.is_archived.is_(False))
    groups = list((await ctx.session.execute(stmt)).scalars())
    sizes = dict(
        (
            await ctx.session.execute(
                select(GroupMembership.group_id, func.count()).group_by(GroupMembership.group_id)
            )
        ).all()
    )
    return {
        "groups": [
            {
                "id": group.id,
                "title": group.title,
                "members": sizes.get(group.id, 0),
                "join_code": group.join_code,
                "archived": group.is_archived,
            }
            for group in groups
        ]
    }


class GroupParams(Params):
    group_id: int


@tool(
    "get_group",
    title=N_("Состав группы"),
    hint=N_("участники и задания группы"),
    roles=TEACHER,
)
async def get_group(ctx: Context, args: GroupParams) -> dict:
    """Группа: участники (номер и имя) и выданные ей задания."""
    group = await ctx.session.get(Group, args.group_id)
    if group is None:
        raise ToolError(f"Группы {args.group_id} нет")
    members = (
        await ctx.session.execute(
            select(User)
            .join(GroupMembership, GroupMembership.user_id == User.id)
            .where(GroupMembership.group_id == group.id)
            .order_by(User.display_name)
        )
    ).scalars()
    issued = (
        await ctx.session.execute(
            select(Assignment)
            .where(Assignment.group_id == group.id)
            .order_by(Assignment.assigned_at.desc())
        )
    ).scalars()
    return {
        "id": group.id,
        "title": group.title,
        "members": [
            {"id": user.id, "name": user.display_name, "official_name": user.official_name,
             "role": user.role.value}
            for user in members
        ],
        "assignments": await _describe(ctx, issued, teacher=True),
    }


@tool(
    "get_assignment_progress",
    title=N_("Матрица задания"),
    hint=N_("кто сколько решил"),
    roles=TEACHER,
)
async def get_assignment_progress(ctx: Context, args: AssignmentParams) -> dict:
    """Матрица задания: по каждому участнику — сколько засчитано и статус каждой задачи.

    Считает так же, как матрица на сайте, табло и выгрузка CSV: засчитано
    (`counts`) — решено в срок, код прислан, если его требуют, и не отклонён.
    """
    assignment = await _assignment_for(ctx, args.assignment_id)
    participants = [
        u for u in await participants_for_assignment(ctx.session, assignment) if not u.is_teacher
    ]
    progress = await compute_progress(ctx.session, assignment, participants)
    rows = []
    for user in participants:
        cells = []
        for problem in progress.problems:
            cell = progress.cell(user.id, problem.id)
            cells.append(
                {
                    "ref": _ref(problem),
                    "status": cell.status.value,
                    "counts": cell.counts,
                    "solved_at": _when(cell.solved_at),
                    "code_review": cell.review.value if cell.review else None,
                    "code_missing": cell.code_missing,
                }
            )
        rows.append(
            {
                "student_id": user.id,
                "student": user.display_name,
                "official_name": user.official_name,
                "solved": progress.solved_count(user.id),
                "problems": cells,
            }
        )
    return {
        **(await _describe(ctx, [assignment], teacher=True))[0],
        "total": progress.total_problems,
        "students": rows,
    }


class SolutionsParams(Params):
    assignment_id: int
    problem: str | None = Field(None, description="Только эта задача задания.")
    student_id: int | None = Field(None, description="Только этот студент.")


@tool(
    "get_assignment_solutions",
    title=N_("Решения студентов"),
    hint=N_("код — для проверки и поиска списывания"),
    roles=TEACHER,
)
async def get_assignment_solutions(ctx: Context, args: SolutionsParams) -> dict:
    """Код решений студентов по заданию — для проверки и поиска списывания.

    По каждой задаче и студенту одно решение: последнее принятое после выдачи
    задания, а если принятого нет — последнее присланное. Источник `upload` —
    код, присланный на портал к задаче с площадки; `submission` — посылка
    (своя задача портала или исходник, подтянутый с площадки). `attempts` —
    сколько посылок было, `late` — после дедлайна.

    Ищешь списывание — сравнивай решения одной задачи между собой: одинаковые
    редкие имена, порядок действий, общие ошибки, время сдачи подряд.
    Похожесть — повод посмотреть, а не приговор: у короткой задачи хорошие
    решения похожи сами по себе. Если ответ обрезан (`truncated`), сужай
    выборку параметрами `problem` или `student_id`.
    """
    assignment = await _assignment_for(ctx, args.assignment_id)
    participants = [
        u for u in await participants_for_assignment(ctx.session, assignment) if not u.is_teacher
    ]
    if args.student_id is not None:
        participants = [u for u in participants if u.id == args.student_id]
        if not participants:
            raise ToolError(f"Студента {args.student_id} нет среди участников задания")
    problems = await problems_for_set(ctx.session, assignment.problem_set_id)
    if args.problem:
        wanted = await _resolve(ctx, args.problem)
        problems = [p for p in problems if p.id == wanted.id]
        if not problems:
            raise ToolError("Этой задачи нет в задании")

    picked = await solutions.picked_for_assignment(
        ctx.session, assignment, [u.id for u in participants], [p.id for p in problems]
    )

    budget = MAX_CODE_CHARS
    truncated = False
    out = []
    for problem in problems:
        entries = []
        for user in participants:
            found = picked.get((user.id, problem.id))
            if found is None:
                continue
            code = found.code
            entry = {
                "student_id": user.id,
                "student": user.display_name,
                "official_name": user.official_name,
                "source": found.source,
                "review": found.review,
                "language": found.language,
                "accepted": found.accepted,
                "submitted_at": _when(found.submitted_at),
                "late": assignment.deadline is not None
                and found.submitted_at > assignment.deadline,
                "attempts": found.attempts,
            }
            if len(code) <= budget:
                entry["code"] = code
                budget -= len(code)
            else:
                entry["code"] = None
                truncated = True
            entries.append(entry)
        out.append({**_problem(problem), "solutions": entries})

    result: dict = {"assignment_id": assignment.id, "problems": out, "truncated": truncated}
    if truncated:
        result["hint"] = (
            "Код части решений не поместился (code = null). Запроси по одной задаче "
            "параметром problem или по студенту параметром student_id."
        )
    return result


class SimilarParams(Params):
    assignment_id: int
    problem: str | None = Field(None, description="Только эта задача задания.")
    across_groups: bool = Field(
        False,
        description="Сравнивать и с решениями той же задачи из других заданий "
        "(других групп, прошлых лет). По умолчанию — только внутри группы.",
    )


@tool(
    "find_similar_solutions",
    title=N_("Похожие решения"),
    hint=N_("пары похожих решений одной задачи — как на странице задания"),
    roles=TEACHER,
)
async def find_similar_solutions(ctx: Context, args: SimilarParams) -> dict:
    """Пары похожих решений по задачам задания — тот же отчёт, что на странице
    «Похожие решения» у задания.

    Сравнивается устройство кода: имена переменных, пробелы и комментарии не
    влияют. `similarity` — проценты совпадения, в отчёт попадают пары от
    порога `threshold`. `shared_names` — имена, которые из всей группы есть
    только у этих двоих: улика сильнее процентов. Короткие решения (`short`)
    не сравниваются — у простой задачи хорошие решения похожи сами по себе.

    По умолчанию — внутри группы. С `across_groups` задача сравнивается и с
    решениями из других заданий — контрольную часто выдают нескольким группам
    сразу. В отчёт идут пары, где хотя бы один — из этого задания; `across` —
    пара из разных групп (её порог выше — `threshold_across`), у студента
    `group` и `assignment_id` — откуда решение.

    Это повод посмотреть, а не приговор. Код пары — get_assignment_solutions
    с параметрами problem и student_id (того задания, откуда решение); ссылку
    `compare_url` дай преподавателю, чтобы он посмотрел решения рядом.
    """
    assignment = await _assignment_for(ctx, args.assignment_id)
    problem_id = None
    if args.problem:
        problem_id = (await _resolve(ctx, args.problem)).id
    reports = await similarity.for_assignment(
        ctx.session, assignment, problem_id, across_groups=args.across_groups
    )
    if problem_id is not None and not reports:
        raise ToolError("Этой задачи нет в задании")
    base = f"/teacher/assignments/{assignment.id}/similar"
    return {
        "assignment_id": assignment.id,
        "threshold": round(similarity.THRESHOLD * 100),
        "threshold_across": round(similarity.ACROSS_THRESHOLD * 100),
        "page_url": portal_url(base),
        "problems": [
            {
                **_problem(report.problem),
                "compared": report.compared,
                "short": report.short,
                "also_compared_with": [
                    {"assignment_id": a.id, "title": a.title,
                     "group": a.group.title if a.group else None}
                    for a in report.others
                ],
                "pairs": [
                    {
                        "students": [
                            {"id": m.first.id, "name": m.first.display_name,
                             "official_name": m.first.official_name,
                             "assignment_id": m.first_from.id,
                             "group": m.first_from.group.title if m.first_from.group else None,
                             "submitted_at": _when(m.first_solution.submitted_at)},
                            {"id": m.second.id, "name": m.second.display_name,
                             "official_name": m.second.official_name,
                             "assignment_id": m.second_from.id,
                             "group": m.second_from.group.title if m.second_from.group else None,
                             "submitted_at": _when(m.second_solution.submitted_at)},
                        ],
                        "similarity": m.pair.percent,
                        "across": m.across,
                        "shared_names": m.pair.shared_names,
                        "compare_url": portal_url(
                            f"{base}/{report.problem.id}?a={m.first.id}&b={m.second.id}"
                        ),
                    }
                    for m in report.matches
                ],
            }
            for report in reports
        ],
    }


def _message(item) -> dict:
    return {
        "id": item.id,
        "text": item.body,
        "at": _when(item.created_at),
        "author": item.author.display_name if item.author else None,
    }


class MessageParams(Params):
    assignment_id: int
    text: str = Field(min_length=1, max_length=messages.MAX_CHARS)
    telegram: bool = Field(False, description="Продублировать в Telegram. По умолчанию — нет.")


@tool(
    "post_assignment_message",
    title=N_("Написать участникам задания"),
    hint=N_("сообщение сразу видно на странице задания и на главной"),
    roles=TEACHER,
    writes=True,
    notifies=True,
)
async def post_assignment_message(ctx: Context, args: MessageParams) -> dict:
    """Сообщение участникам задания — как поле «Сообщения преподавателя» на
    странице задания: ссылка на комнату для совместного решения, «смотрите
    задачу 3». Студенты сразу видят его на странице задания и несколько
    часов — на главной; в Telegram — только с telegram=true.

    Перед вызовом покажи человеку текст и кому он уйдёт, дождись явного
    «да»: отозвать увиденное нельзя (удалить можно — на сайте).
    """
    assignment = await _assignment_for(ctx, args.assignment_id)
    result = await messages.post(
        ctx.session, assignment, ctx.user, args.text, telegram=args.telegram
    )
    if isinstance(result, str):
        raise ToolError(result)
    return {
        "assignment_id": assignment.id,
        "message": _message(result),
        "url": portal_url(f"/assignments/{assignment.id}#messages"),
    }


class CreateAssignmentParams(Params):
    title: str = Field(min_length=1, max_length=200)
    problem_set_id: int
    group_ids: list[int] = Field(
        default_factory=list,
        description="Кому: по заданию на каждую группу. Пусто — только вместе с for_everyone.",
    )
    for_everyone: bool = Field(
        False, description="Выдать всем студентам портала, без групп. Нужно сказать явно."
    )
    description: str = Field("", max_length=2000)
    starts_at: datetime | None = Field(
        None,
        description=(
            "Начало, ISO 8601. Без пояса — местное время портала. Пусто — сейчас. "
            "До начала студенты задание не видят, сообщение в Telegram уйдёт в момент начала."
        ),
    )
    deadline: datetime | None = Field(
        None, description="Дедлайн, ISO 8601. Без пояса — местное время портала."
    )
    requires_solution: bool = Field(
        False,
        description="Требовать код решения задач с площадок: без него зачёта нет.",
    )
    count_prior_solves: bool = Field(
        False, description="Засчитывать задачи, решённые до выдачи задания."
    )


@tool(
    "create_assignment",
    title=N_("Выдать задание"),
    hint=N_("студенты сразу видят его, в Telegram уходит сообщение"),
    roles=TEACHER,
    writes=True,
    notifies=True,
)
async def create_assignment(ctx: Context, args: CreateAssignmentParams) -> dict:
    """Выдаёт список задач группам — студенты сразу видят задание, а в Telegram
    уходит сообщение. Отменить рассылку нельзя.

    Перед вызовом покажи человеку название, список, группы, начало и дедлайн
    и дождись явного «да». После выдачи меняются только название, описание и
    дедлайн (вперёд) — на сайте; задачи — remove_problems_from_set и
    add_problems_to_set.

    Начало в будущем: до него студенты не видят задание нигде (ни на портале,
    ни через MCP), а сообщение в Telegram уходит в момент начала —
    `telegram_messages` тогда 0, это не ошибка.
    """
    if not args.group_ids and not args.for_everyone:
        raise ToolError(
            "Не указано, кому выдать: передай group_ids (номера из list_groups) "
            "или for_everyone=true, если задание действительно для всех."
        )
    try:
        created, delivered = await assignments_service.issue(
            ctx.session,
            ctx.user,
            title=args.title,
            problem_set_id=args.problem_set_id,
            group_ids=[] if args.for_everyone else args.group_ids,
            description=args.description,
            starts_at=_moment(args.starts_at),
            deadline=_moment(args.deadline),
            count_prior_solves=args.count_prior_solves,
            requires_solution=args.requires_solution,
        )
    except assignments_service.AssignmentError as exc:
        raise ToolError(str(exc)) from exc
    return {
        "assignments": await _describe(ctx, created, teacher=True),
        "telegram_messages": delivered,
    }


# ---------------------------------------------------------------- оценки
#
# Раздел «Оценки» закрывается в «Видимости разделов» — тогда эти инструменты
# пропадают у той же роли, что и страница (`section="grades"`).


def _mark(column, value: sheet.Value) -> dict:
    """Ячейка ведомости как её видит человек на странице."""
    out: dict = {"comment": value.comment or None}
    if column.kind == SheetKind.attendance:
        out["attendance"] = value.attendance.value if value.attendance else None
    elif column.kind == SheetKind.assignment:
        out.update(solved=value.solved, total=value.total, passed=value.passed)
    elif column.scale == SheetScale.points:
        out.update(points=value.points, max_points=column.max_points)
    else:
        out["passed"] = value.passed
    return out


def _columns(book: sheet.Sheet) -> list[tuple[dict, object]]:
    """Колонки по порядку ведомости вместе с занятием, к которому относятся."""
    out = []
    for block in book.blocks:
        for column in block.columns:
            out.append(
                (
                    {
                        "column_id": column.id,
                        "lesson": block.title or None,
                        "held_on": _when(block.lesson.held_on) if block.lesson else None,
                        "title": column.title,
                        "kind": column.kind.value,
                    },
                    column,
                )
            )
    return out


def _total(total: sheet.Total) -> dict:
    return {
        "points": total.points,
        "points_max": total.points_max,
        "passed": total.passed,
        "gradable": total.gradable,
        "not_graded_yet": total.ungraded,
        "present": total.present,
        "lessons_marked": total.lessons,
    }


@tool(
    "get_my_grades",
    title=N_("Мои оценки"),
    hint=N_("посещения, оценки и комментарии по группам"),
    roles=STUDENT_ONLY,
    section="grades",
)
async def get_my_grades(ctx: Context, args: NoParams) -> dict:
    """Своя строка ведомости по каждой группе — то же, что в разделе «Оценки».

    По работам: посещение (present / absent / excused), зачёт (`passed`),
    баллы из максимума или решённые задачи задания; `comment` — пометка
    преподавателя. Пустое значение — работу ещё не оценили, это не двойка.
    Итог — три отдельных числа: баллы, зачёты и посещаемость; одной итоговой
    оценки портал не считает.
    """
    groups = []
    for group, book in await sheet.my_rows(ctx.session, ctx.user):
        works = [
            {**meta, **_mark(column, book.value(ctx.user.id, column.id))}
            for meta, column in _columns(book)
        ]
        groups.append(
            {"group": group.title, "works": works, "total": _total(book.total(ctx.user.id))}
        )
    return {"groups": groups}


@tool(
    "get_gradebook",
    title=N_("Ведомость группы"),
    hint=N_("все студенты группы по всем работам"),
    roles=TEACHER,
    section="grades",
)
async def get_gradebook(ctx: Context, args: GroupParams) -> dict:
    """Ведомость группы: колонки по порядку занятий и строка каждого студента.

    Значения те же, что на странице ведомости и в её CSV: колонки-задания
    считаются по правилу зачёта портала, остальное ставит преподаватель.
    Пустое значение — работу ещё не оценили.
    """
    group = await ctx.session.get(Group, args.group_id)
    if group is None:
        raise ToolError(f"Группы {args.group_id} нет")
    book = await sheet.build(ctx.session, group)
    columns = _columns(book)
    return {
        "group": group.title,
        "columns": [meta for meta, _ in columns],
        "students": [
            {
                "student_id": student.id,
                "student": student.display_name,
                "official_name": student.official_name,
                "marks": [
                    {
                        "column_id": meta["column_id"],
                        **_mark(column, book.value(student.id, column.id)),
                    }
                    for meta, column in columns
                ],
                "total": _total(book.total(student.id)),
            }
            for student in book.students
        ],
    }


# ---------------------------------------------------------------- подсказка модели


STUDENT_GUIDE = """\
Портал тренировок по программированию. Ты подключён от имени студента и
видишь только его: его задания, задачи из них, его зачёт и объявления.
Изменить через этот сервер ничего нельзя — решения сдаются на сайте.

Начни с list_assignments: что выдано, до какого срока и сколько засчитано.
Подробности задания — get_assignment, условие задачи — get_problem.
Задача засчитана (counts), если решена после выдачи и до дедлайна, а если
задание требует код — код прислан и не отклонён. Закрытых тестов нет ни у
кого, кроме портала. Время — в поясе из whoami.
"""

TEACHER_GUIDE = """\
Портал тренировок по программированию. Ты подключён от имени преподавателя.

Подготовка семинара или контрольной:
1. list_problem_sets — может, подходящий список уже есть.
2. search_problems — подбор по меткам, сложности LeetCode, рейтингу Codeforces.
   В список попадает только то, что есть в каталоге портала.
3. preview_problem_list — пробный разбор: что узнано, что нет, где задача уже
   стояла. Покажи итог человеку: повтор задачи, которую группа решала, он мог
   не заметить.
4. create_problem_set — после согласия человека.
5. list_groups, затем create_assignment — только после явного «да»: студенты
   сразу видят задание, в Telegram уходит сообщение.

На паре: post_assignment_message — ссылка на комнату или пара слов участникам
задания; видно сразу на странице задания и на главной. Тоже только после «да».

Проверка: get_assignment_progress — кто сколько решил (так же, как матрица
и табло на сайте); get_assignment_solutions — код решений для проверки и
поиска списывания; find_similar_solutions — готовые пары похожих решений,
как на странице «Похожие решения». Похожесть — повод посмотреть, а не приговор.

Записи задач: ссылка, слаг LeetCode (two-sum), cf:4A, task:слаг своей задачи.
Время без пояса — местное время портала (см. whoami).
"""

READ_ONLY_NOTE = """
Ключ выпущен только на чтение: создать список или выдать задание нельзя.
Нужно — выпусти на странице «ИИ» ключ с разрешением на изменения.
"""


def instructions(ctx: Context) -> str:
    if not ctx.user.is_teacher:
        return STUDENT_GUIDE
    return TEACHER_GUIDE + ("" if ctx.can_write else READ_ONLY_NOTE)
