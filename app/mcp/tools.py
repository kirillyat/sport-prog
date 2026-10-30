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
    BonusPoint,
    Contest,
    ContestRules,
    Group,
    GroupMembership,
    Platform,
    PlatformAccount,
    Problem,
    ProblemSet,
    ProblemSetItem,
    ReviewStatus,
    SheetKind,
    SheetScale,
    SolutionUpload,
    Task,
    TaskTest,
    User,
    utcnow,
)
from app.notify import portal_url
from app.routers.announcements import list_visible
from app.services import announcements as announcements_service
from app.services import assignments as assignments_service
from app.services import contests as contests_service
from app.services import deadlines, messages, problem_sets, sheet, similarity, solutions
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
    и дождись явного «да». После выдачи меняются только название и описание
    (edit_assignment), дедлайн — вперёд (extend_deadline), задачи —
    remove_problems_from_set и add_problems_to_set.

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


class ExtendDeadlineParams(Params):
    assignment_ids: list[int] = Field(
        min_length=1,
        max_length=50,
        description="Номера заданий из list_assignments. Контрольная на несколько групп — "
        "это несколько заданий, по одному на группу.",
    )
    deadline: datetime | None = Field(
        None, description="Новый дедлайн, ISO 8601. Без пояса — местное время портала."
    )
    remove_deadline: bool = Field(
        False, description="Снять дедлайн совсем вместо переноса. Нужно сказать явно."
    )
    dry_run: bool = Field(
        False,
        description="Только посчитать, что изменится, и ничего не записывать.",
    )


@tool(
    "extend_deadline",
    title=N_("Перенести дедлайн"),
    hint=N_("только вперёд; зачёт пересчитывается сразу"),
    roles=TEACHER,
    writes=True,
)
async def extend_deadline(ctx: Context, args: ExtendDeadlineParams) -> dict:
    """Переносит дедлайн заданий вперёд или снимает его — как форма «Дедлайн»
    на странице задания, но сразу для нескольких.

    Только вперёд: ранний срок задним числом снял бы зачёт с тех, кто решил
    честно. Заданию без дедлайна срок не ставится. Всё или ничего: если хоть
    одно задание отказало, не меняется ни одно — причина придёт в ошибке.

    Зачёт считается из посылок и дедлайна, поэтому решённое между старым и
    новым сроком сразу станет «в срок» на табло, в матрице и в CSV:
    `credits_before` → `credits_after` — сколько зачётов было и станет у
    студентов. Бот напомнит о новом сроке заново; отдельного сообщения
    в Telegram не уходит — хочешь сказать группе, post_assignment_message.

    Порядок: вызови с dry_run=true, покажи человеку задания, старый и новый
    срок и прирост зачётов, дождись явного «да» — и вызови без dry_run.
    """
    if (args.deadline is None) == (not args.remove_deadline):
        raise ToolError("Передай либо deadline (новый срок), либо remove_deadline=true.")
    new_deadline = None if args.remove_deadline else _moment(args.deadline)
    shifts = await deadlines.extend_many(ctx.session, args.assignment_ids, new_deadline)

    failed = [shift for shift in shifts if shift.error]
    if failed:
        lines = [
            f"задание {shift.assignment_id}"
            + (f" «{shift.assignment.title}»" if shift.assignment else "")
            + f": {shift.error}"
            for shift in failed
        ]
        raise ToolError("Ничего не изменено. " + "; ".join(lines))

    # Описание собираем до записи: после commit поля заданий сброшены,
    # а подгружать их заново в асинхронной сессии нельзя.
    groups = await _group_titles(ctx, (shift.assignment.group_id for shift in shifts))
    out = {
        "dry_run": args.dry_run,
        "new_deadline": _when(new_deadline),
        "assignments": [
            {
                "id": shift.assignment_id,
                "title": shift.assignment.title,
                "for": _target(shift.assignment.group_id, shift.assignment.user_id, groups),
                "old_deadline": _when(shift.old_deadline),
                "new_deadline": _when(new_deadline),
                "credits_before": shift.credits_before,
                "credits_after": shift.credits_after,
                "url": portal_url(f"/teacher/assignments/{shift.assignment_id}"),
            }
            for shift in shifts
        ],
    }
    if args.dry_run:
        await ctx.session.rollback()
    else:
        await ctx.session.commit()
    return out


class EditAssignmentParams(Params):
    assignment_id: int
    title: str | None = Field(None, max_length=200, description="Новое название. Пусто — прежнее.")
    description: str | None = Field(
        None, max_length=2000, description="Новое описание. Пустая строка — убрать описание."
    )


@tool(
    "edit_assignment",
    title=N_("Переименовать задание"),
    hint=N_("название и описание; задачи и кому — нет"),
    roles=TEACHER,
    writes=True,
)
async def edit_assignment(ctx: Context, args: EditAssignmentParams) -> dict:
    """Меняет название и описание выданного задания — как форма на его странице.

    Больше после выдачи ничего не правится: кому выдано и правила зачёта
    задаются при создании. Срок — extend_deadline, задачи —
    add_problems_to_set и remove_problems_from_set.
    """
    if args.title is None and args.description is None:
        raise ToolError("Нечего менять: передай title или description.")
    assignment = await _assignment_for(ctx, args.assignment_id)
    try:
        await assignments_service.edit(
            ctx.session,
            assignment,
            title=assignment.title if args.title is None else args.title,
            description=(
                (assignment.description or "") if args.description is None else args.description
            ),
        )
    except assignments_service.AssignmentError as exc:
        raise ToolError(str(exc)) from exc
    await ctx.session.refresh(assignment)
    return (await _describe(ctx, [assignment], teacher=True))[0]


# ---------------------------------------------------------------- преподавателю: проверка кода


class PendingReviewsParams(Params):
    assignment_id: int | None = Field(None, description="Только это задание.")


@tool(
    "list_pending_reviews",
    title=N_("Очередь проверки"),
    hint=N_("присланный код, который ждёт проверки"),
    roles=TEACHER,
)
async def list_pending_reviews(ctx: Context, args: PendingReviewsParams) -> dict:
    """Код, который студенты прислали к задачам с площадок и который ждёт
    проверки, — то же, что страница «Проверка». Сначала присланное раньше.

    Принять или отклонить — review_solution с теми же assignment_id,
    problem и student_id. Если ответ обрезан (`truncated`), сужай выборку
    параметром assignment_id.
    """
    waiting = await solutions.pending(ctx.session, limit=200)
    if args.assignment_id is not None:
        waiting = [upload for upload in waiting if upload.assignment_id == args.assignment_id]
    users = {
        user.id: user
        for user in (
            await ctx.session.execute(
                select(User).where(User.id.in_({upload.user_id for upload in waiting}))
            )
        ).scalars()
    }
    problems = {
        problem.id: problem
        for problem in (
            await ctx.session.execute(
                select(Problem).where(Problem.id.in_({upload.problem_id for upload in waiting}))
            )
        ).scalars()
    }
    titles = dict(
        (
            await ctx.session.execute(
                select(Assignment.id, Assignment.title).where(
                    Assignment.id.in_({upload.assignment_id for upload in waiting})
                )
            )
        ).all()
    )
    budget = MAX_CODE_CHARS
    truncated = False
    out = []
    for upload in waiting:
        student = users[upload.user_id]
        entry = {
            "assignment_id": upload.assignment_id,
            "assignment": titles.get(upload.assignment_id),
            "problem": _ref(problems[upload.problem_id]),
            "problem_title": problems[upload.problem_id].title,
            "student_id": student.id,
            "student": student.display_name,
            "official_name": student.official_name,
            "submitted_at": _when(upload.submitted_at),
            "url": portal_url(f"/solutions/{upload.id}"),
        }
        if len(upload.code) <= budget:
            entry["code"] = upload.code
            budget -= len(upload.code)
        else:
            entry["code"] = None
            truncated = True
        out.append(entry)
    return {"pending": out, "truncated": truncated}


class ReviewParams(Params):
    assignment_id: int
    problem: str = Field(description="Задача задания: ссылка, слаг, cf:4A или task:слаг.")
    student_id: int
    decision: Literal["accept", "reject"]
    comment: str = Field(
        "",
        max_length=500,
        description="Что не так и что исправить. Студент его увидит. Для reject обязателен.",
    )


@tool(
    "review_solution",
    title=N_("Принять или отклонить код"),
    hint=N_("отклонённый код снимает зачёт по задаче"),
    roles=TEACHER,
    writes=True,
)
async def review_solution(ctx: Context, args: ReviewParams) -> dict:
    """Принимает или отклоняет код, присланный студентом к задаче, — как
    кнопки на странице решения. Проверяется только присланный код (`source`
    = upload в get_assignment_solutions), посылки с площадок не проверяются.

    Отклонение снимает зачёт по задаче, пока студент не пришлёт код заново;
    без комментария отклонить нельзя — студент должен понять, что исправить.
    Перед отклонением покажи человеку студента, задачу и комментарий и
    дождись явного «да»: подозрение на списывание — повод посмотреть, а не
    приговор.
    """
    assignment = await _assignment_for(ctx, args.assignment_id)
    problem = await _resolve(ctx, args.problem)
    upload = await ctx.session.scalar(
        select(SolutionUpload).where(
            SolutionUpload.assignment_id == assignment.id,
            SolutionUpload.problem_id == problem.id,
            SolutionUpload.user_id == args.student_id,
        )
    )
    if upload is None:
        raise ToolError(
            "Студент не присылал код к этой задаче в этом задании — проверять нечего. "
            "Проверяется только присланный код (source = upload)."
        )
    status = ReviewStatus.accepted if args.decision == "accept" else ReviewStatus.rejected
    error = await solutions.review(
        ctx.session, upload, status=status, comment=args.comment, reviewer_id=ctx.user.id
    )
    if error:
        raise ToolError(error)
    await ctx.session.refresh(upload)
    return {
        "assignment_id": upload.assignment_id,
        "problem": _ref(problem),
        "student_id": upload.user_id,
        "status": upload.status.value,
        "comment": upload.comment,
        "url": portal_url(f"/solutions/{upload.id}"),
    }


# ---------------------------------------------------------------- преподавателю: объявления


class CreateAnnouncementParams(Params):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field("", max_length=4000)
    url: str = Field("", max_length=500, description="Ссылка на контест или регистрацию.")
    url_label: str = Field("", max_length=80, description="Подпись кнопки со ссылкой.")
    group_id: int | None = Field(None, description="Только этой группе (номер из list_groups).")
    for_everyone: bool = Field(
        False, description="Всем студентам портала, без группы. Нужно сказать явно."
    )
    starts_at: datetime | None = Field(
        None,
        description="Когда событие начнётся — к нему идёт обратный отсчёт. ISO 8601, "
        "без пояса — местное время портала.",
    )
    ends_at: datetime | None = Field(None, description="Когда событие закончится.")
    pinned: bool = Field(False, description="Закрепить сверху.")


@tool(
    "create_announcement",
    title=N_("Опубликовать объявление"),
    hint=N_("видно в «Анонсах», в Telegram уходит сообщение"),
    roles=TEACHER,
    writes=True,
    notifies=True,
)
async def create_announcement(ctx: Context, args: CreateAnnouncementParams) -> dict:
    """Публикует объявление — контест, сбор, организационную новость — как
    форма в разделе «Анонсы». Студенты сразу видят его на портале, в Telegram
    уходит сообщение. Отменить рассылку нельзя (удалить объявление — на сайте).

    Перед вызовом покажи человеку заголовок, текст, кому и даты и дождись
    явного «да». Про конкретное задание лучше post_assignment_message: оно
    приходит только его участникам.
    """
    if (args.group_id is None) == (not args.for_everyone):
        raise ToolError(
            "Укажи, кому: group_id (номер из list_groups) или for_everyone=true, "
            "если объявление действительно для всех."
        )
    try:
        item, delivered = await announcements_service.publish(
            ctx.session,
            ctx.user,
            title=args.title,
            body=args.body,
            url=args.url,
            url_label=args.url_label,
            group_id=args.group_id,
            starts_at=_moment(args.starts_at),
            ends_at=_moment(args.ends_at),
            pinned=args.pinned,
        )
    except announcements_service.AnnouncementError as exc:
        raise ToolError(str(exc)) from exc
    groups = await _group_titles(ctx, [item.group_id])
    return {
        "id": item.id,
        "title": item.title,
        "for": groups.get(item.group_id, "") if item.group_id else "все",
        "starts_at": _when(item.starts_at),
        "telegram_messages": delivered,
        "url": portal_url("/announcements"),
    }


# ---------------------------------------------------------------- преподавателю: студенты


class FindStudentsParams(Params):
    query: str = Field(
        min_length=2, max_length=120, description="Часть имени на портале или в вузе."
    )


@tool(
    "find_students",
    title=N_("Найти студента"),
    hint=N_("по имени или фамилии, с группами"),
    roles=TEACHER,
)
async def find_students(ctx: Context, args: FindStudentsParams) -> dict:
    """Студенты, у которых в имени на портале или в имени из учётной записи
    вуза есть `query`; регистр не важен. Номер — для get_student и других
    инструментов. Не больше 30 совпадений: много — уточни запрос.
    """
    # Без LIKE в базе: SQLite сравнивает без регистра только латиницу,
    # а «петров» должен находить «Петров».
    needle = args.query.strip().casefold()
    people = (
        await ctx.session.execute(
            select(User).where(User.is_active.is_(True)).order_by(User.display_name)
        )
    ).scalars()
    found = [
        user for user in people
        if needle in user.display_name.casefold()
        or needle in (user.official_name or "").casefold()
    ][:30]
    memberships = (
        await ctx.session.execute(
            select(GroupMembership.user_id, Group.title)
            .join(Group, Group.id == GroupMembership.group_id)
            .where(
                GroupMembership.user_id.in_([user.id for user in found]),
                Group.is_archived.is_(False),
            )
        )
    ).all()
    groups: dict[int, list[str]] = {}
    for user_id, title in memberships:
        groups.setdefault(user_id, []).append(title)
    return {
        "students": [
            {
                "id": user.id,
                "name": user.display_name,
                "official_name": user.official_name,
                "role": user.role.value,
                "groups": sorted(groups.get(user.id, [])),
            }
            for user in found
        ]
    }


class StudentParams(Params):
    student_id: int = Field(description="Номер из find_students или get_group.")


@tool(
    "get_student",
    title=N_("Студент целиком"),
    hint=N_("группы, аккаунты и зачёт по всем заданиям"),
    roles=TEACHER,
)
async def get_student(ctx: Context, args: StudentParams) -> dict:
    """Всё о студенте на одной странице: группы, аккаунты на площадках
    (подтверждены ли, когда обновлялись), бонусные баллы и по каждому
    начавшемуся заданию — сколько засчитано из скольких.

    Зовут на «почему у него ноль» и «как дела у Петрова»: неподтверждённый
    аккаунт или давнее обновление — частая причина пустой строки.
    Подробности задания — get_assignment_progress.
    """
    student = await ctx.session.get(User, args.student_id)
    if student is None:
        raise ToolError(f"Человека {args.student_id} нет")
    accounts = (
        await ctx.session.execute(
            select(PlatformAccount).where(PlatformAccount.user_id == student.id)
        )
    ).scalars()
    bonus = await ctx.session.scalar(
        select(func.coalesce(func.sum(BonusPoint.points), 0)).where(
            BonusPoint.user_id == student.id
        )
    )
    mine = await assignments_for_user(ctx.session, student)
    items = await _describe(ctx, mine, teacher=True)
    for item, assignment in zip(items, mine, strict=True):
        progress = await compute_progress(ctx.session, assignment, [student])
        item.update(solved=progress.solved_count(student.id), total=progress.total_problems)
    return {
        "id": student.id,
        "name": student.display_name,
        "official_name": student.official_name,
        "role": student.role.value,
        "active": student.is_active,
        "groups": [
            {"id": group.id, "title": group.title}
            for group in await groups_for_user(ctx.session, student)
        ],
        "accounts": [
            {
                "platform": account.platform.value,
                "handle": account.handle,
                "verified": account.verified_at is not None,
                "last_synced_at": _when(account.last_synced_at),
            }
            for account in accounts
        ],
        "bonus_points": float(bonus or 0),
        "assignments": items,
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
create_announcement — объявление группе или всем (контест, сбор), уходит в
Telegram; только после «да».

Сроки: extend_deadline — перенос вперёд или снятие, сразу для нескольких
заданий. Сначала dry_run=true: покажи человеку, сколько зачётов прибавится,
и запиши только после «да». edit_assignment — название и описание.

Проверка: get_assignment_progress — кто сколько решил (так же, как матрица
и табло на сайте); get_assignment_solutions — код решений для проверки и
поиска списывания; find_similar_solutions — готовые пары похожих решений,
как на странице «Похожие решения». Похожесть — повод посмотреть, а не приговор.
list_pending_reviews — присланный код, который ждёт проверки; review_solution —
принять или отклонить (отклонение снимает зачёт — только после «да»).

Студенты: find_students — найти по имени; get_student — группы, аккаунты на
площадках и зачёт по всем заданиям. Это первый шаг на «почему у него ноль».

Соревнования (раздел «Соревнования», отдельно от заданий и зачёта):
list_contests, get_contest — настройки, задачи по буквам, команды с кодами;
create_contest — черновик, студенты его не видят; update_contest — настройки
и задачи (задачи — только свои, task:слаг, и только до старта);
publish_contest — открыть регистрацию; start_contest — «Старт», задачи сразу
видны всем участникам; extend_contest и finish_contest — время тура.
Публикацию и старт — только после явного «да». get_contest_standings — табло,
преподавателю живое даже в заморозку.

Записи задач: ссылка, слаг LeetCode (two-sum), cf:4A, task:слаг своей задачи.
Время без пояса — местное время портала (см. whoami).
"""

READ_ONLY_NOTE = """
Ключ выпущен только на чтение: создать список, выдать задание, перенести
срок или написать студентам нельзя.
Нужно — выпусти на странице «ИИ» ключ с разрешением на изменения.
"""


def instructions(ctx: Context) -> str:
    if not ctx.user.is_teacher:
        return STUDENT_GUIDE
    return TEACHER_GUIDE + ("" if ctx.can_write else READ_ONLY_NOTE)


# ---------------------------------------------------------------- соревнования
#
# Тот же сервис, что у страниц раздела `/contest`. Раздел закрыт для роли —
# закрыты и инструменты (`section="contest"`).


def _contest(contest: Contest, *, teacher: bool) -> dict:
    out = {
        "id": contest.id,
        "title": contest.title,
        "description": contest.description,
        "phase": contest.phase(),
        "rules": contest.rules.value,
        "team_size": contest.team_size,
        "duration_min": contest.duration_min,
        "freeze_min": contest.freeze_min,
        "planned_at": _when(contest.planned_at),
        "started_at": _when(contest.started_at),
        "ends_at": _when(contest.ends_at),
        "url": portal_url(f"/contest/{contest.id}"),
    }
    if teacher or contest.started_at is not None:
        out["problems"] = [
            {"letter": item.letter, "ref": _ref(item.problem), "title": item.problem.title}
            for item in contest.problems
        ]
    return out


async def _contest_for(ctx: Context, contest_id: int) -> Contest:
    contest = await ctx.session.get(Contest, contest_id)
    if contest is None or not contests_service.may_see(contest, ctx.user):
        raise ToolError(f"Соревнования {contest_id} нет")
    return contest


class ContestParams(Params):
    contest_id: int = Field(description="Номер соревнования из list_contests.")


@tool(
    "list_contests",
    title=N_("Соревнования"),
    hint=N_("соревнования портала и участие в них"),
    roles=STUDENT,
    section="contest",
)
async def list_contests(ctx: Context, args: NoParams) -> dict:
    """Соревнования портала, свежие сверху. `phase`: draft (черновик, видит
    только преподаватель), registration, running, finished. `my_team` —
    команда того, чей ключ, если он зарегистрирован."""
    out = []
    for contest in await contests_service.visible(ctx.session, ctx.user):
        item = _contest(contest, teacher=ctx.user.is_teacher)
        team = await contests_service.team_of(ctx.session, contest, ctx.user)
        item["my_team"] = team.name if team else None
        out.append(item)
    return {"contests": out}


@tool(
    "get_contest",
    title=N_("Соревнование"),
    hint=N_("настройки, задачи и команды с кодами"),
    roles=TEACHER,
    section="contest",
)
async def get_contest(ctx: Context, args: ContestParams) -> dict:
    """Соревнование целиком: настройки, задачи по буквам и команды с составом
    и кодом приглашения. Код можно продиктовать студенту, потерявшему его."""
    contest = await _contest_for(ctx, args.contest_id)
    teams = await contests_service.teams(ctx.session, contest)
    return {
        **_contest(contest, teacher=True),
        "teams": [
            {
                "id": team.id,
                "name": team.name,
                "join_code": team.join_code,
                "members": [member.user.display_name for member in team.members],
            }
            for team in teams
        ],
    }


class ContestSettings(Params):
    title: str = Field(min_length=1, max_length=200)
    rules: Literal["icpc", "ioi"] = Field(
        "icpc", description="icpc — решённые задачи и штраф; ioi — баллы за тесты."
    )
    team_size: int = Field(3, ge=1, le=10, description="1 — личное соревнование.")
    duration_min: int = Field(300, ge=1, le=1440)
    freeze_min: int = Field(
        0, ge=0, description="За сколько минут до конца заморозить табло; 0 — не замораживать."
    )
    planned_at: datetime | None = Field(
        None,
        description="Плановое начало, ISO 8601 — только подпись. Задачи открывает start_contest.",
    )
    description: str = ""
    problems: list[str] | None = Field(
        None,
        max_length=26,
        description="Свои задачи по порядку (A, B, …): task:слаг или слаг. "
        "Не передано — задачи не трогаются.",
    )


@tool(
    "create_contest",
    title=N_("Создать соревнование"),
    hint=N_("черновик: студенты его не видят"),
    roles=TEACHER,
    writes=True,
    section="contest",
)
async def create_contest(ctx: Context, args: ContestSettings) -> dict:
    """Создаёт черновик соревнования — студенты его не видят до publish_contest.
    Задачи — только свои задачи портала с тестами (list_own_tasks)."""
    try:
        contest = await contests_service.create(
            ctx.session,
            ctx.user,
            title=args.title,
            rules=ContestRules(args.rules),
            team_size=args.team_size,
            duration_min=args.duration_min,
            freeze_min=args.freeze_min,
            planned_at=_moment(args.planned_at),
            description=args.description,
        )
        if args.problems:
            await contests_service.set_problems(ctx.session, contest, args.problems)
    except contests_service.ContestError as exc:
        raise ToolError(str(exc)) from exc
    return _contest(contest, teacher=True)


class UpdateContestParams(ContestSettings):
    contest_id: int


@tool(
    "update_contest",
    title=N_("Изменить соревнование"),
    hint=N_("настройки и задачи; задачи — только до старта"),
    roles=TEACHER,
    writes=True,
    section="contest",
)
async def update_contest(ctx: Context, args: UpdateContestParams) -> dict:
    """Меняет настройки — как форма на пульте. После старта правила, размер
    команды, длительность и задачи не меняются: время двигают extend_contest
    и finish_contest."""
    contest = await _contest_for(ctx, args.contest_id)
    try:
        await contests_service.update(
            ctx.session,
            contest,
            title=args.title,
            rules=ContestRules(args.rules),
            team_size=args.team_size,
            duration_min=args.duration_min,
            freeze_min=args.freeze_min,
            planned_at=_moment(args.planned_at),
            description=args.description,
        )
        if args.problems is not None:
            await contests_service.set_problems(ctx.session, contest, args.problems)
    except contests_service.ContestError as exc:
        raise ToolError(str(exc)) from exc
    return _contest(contest, teacher=True)


@tool(
    "publish_contest",
    title=N_("Открыть регистрацию"),
    hint=N_("студенты видят соревнование и регистрируются"),
    roles=TEACHER,
    writes=True,
    section="contest",
)
async def publish_contest(ctx: Context, args: ContestParams) -> dict:
    """Публикует черновик: студенты видят соревнование и регистрируются.
    Задачи до старта по-прежнему закрыты. Перед вызовом дождись «да»."""
    contest = await _contest_for(ctx, args.contest_id)
    try:
        await contests_service.publish(ctx.session, contest)
    except contests_service.ContestError as exc:
        raise ToolError(str(exc)) from exc
    return _contest(contest, teacher=True)


@tool(
    "start_contest",
    title=N_("Старт соревнования"),
    hint=N_("задачи сразу видны всем, идут часы"),
    roles=TEACHER,
    writes=True,
    section="contest",
)
async def start_contest(ctx: Context, args: ContestParams) -> dict:
    """Кнопка «Старт»: задачи сразу открываются всем участникам, и идут часы
    на duration_min. Отменить нельзя. Зови только по явному «да» в разговоре
    — обычно преподаватель жмёт кнопку в аудитории сам."""
    contest = await _contest_for(ctx, args.contest_id)
    try:
        await contests_service.start(ctx.session, contest)
    except contests_service.ContestError as exc:
        raise ToolError(str(exc)) from exc
    return _contest(contest, teacher=True)


class ExtendContestParams(ContestParams):
    minutes: int = Field(ge=1, le=1440, description="Сколько минут добавить к концу.")


@tool(
    "extend_contest",
    title=N_("Продлить тур"),
    hint=N_("сдвигает конец соревнования"),
    roles=TEACHER,
    writes=True,
    section="contest",
)
async def extend_contest(ctx: Context, args: ExtendContestParams) -> dict:
    """Добавляет минуты к концу тура. После конца — продолжает тур с текущего момента."""
    contest = await _contest_for(ctx, args.contest_id)
    try:
        await contests_service.extend(ctx.session, contest, args.minutes)
    except contests_service.ContestError as exc:
        raise ToolError(str(exc)) from exc
    return _contest(contest, teacher=True)


@tool(
    "finish_contest",
    title=N_("Завершить тур"),
    hint=N_("конец сейчас, табло размораживается"),
    roles=TEACHER,
    writes=True,
    section="contest",
)
async def finish_contest(ctx: Context, args: ContestParams) -> dict:
    """Заканчивает тур сейчас: табло размораживается, дальше — дорешивание."""
    contest = await _contest_for(ctx, args.contest_id)
    try:
        await contests_service.finish(ctx.session, contest)
    except contests_service.ContestError as exc:
        raise ToolError(str(exc)) from exc
    return _contest(contest, teacher=True)


@tool(
    "get_contest_standings",
    title=N_("Табло соревнования"),
    hint=N_("места, решённые задачи, штраф или баллы"),
    roles=STUDENT,
    section="contest",
)
async def get_contest_standings(ctx: Context, args: ContestParams) -> dict:
    """Табло — то же, что на странице. Студенту в заморозку — замороженное
    (`frozen`: true, `pending` — посылки с неизвестным пока итогом),
    преподавателю — живое. До старта табло нет."""
    contest = await _contest_for(ctx, args.contest_id)
    if not contests_service.problems_open(contest, ctx.user):
        raise ToolError("Соревнование ещё не началось — табло появится после старта")
    board = await contests_service.standings(
        ctx.session, contest, live=ctx.user.is_teacher
    )
    return {
        "contest": contest.title,
        "rules": contest.rules.value,
        "frozen": board.frozen,
        "problems": [item.letter for item in board.problems],
        "rows": [
            {
                "place": row.place,
                "team": row.team.name,
                "members": [member.user.display_name for member in row.team.members],
                "solved": row.solved,
                "penalty": row.penalty,
                "score": row.score,
                "cells": {
                    item.letter: {
                        "solved": (cell := row.cell(item.problem_id)).solved,
                        "tries": cell.tries,
                        "minute": cell.minute,
                        "score": cell.score,
                        "pending": cell.pending,
                    }
                    for item in board.problems
                },
            }
            for row in board.rows
        ],
    }
