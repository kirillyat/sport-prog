"""Соревнования: настройка, команды, старт по кнопке, посылки и табло.

Здесь, а не в обработчиках страниц, потому что то же самое делает
преподаватель через нейросеть по MCP, — и правила у обоих путей одни.

Табло считается из посылок при каждом запросе, а не хранится: посылок за
соревнование — сотни, а сохранённое табло пришлось бы пересчитывать при каждом
«+время», заморозке и снятой задаче. Две правды об одном — хуже одной.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.i18n import translate as _
from app.models import (
    Contest,
    ContestMember,
    ContestProblem,
    ContestRules,
    ContestRun,
    ContestTeam,
    Platform,
    Problem,
    ProblemSetItem,
    Task,
    User,
    utcnow,
)

# Штраф ICPC за каждую отклонённую посылку до принятой, в минутах.
PENALTY_MIN = 20
# Без потолка «без ограничения» означало бы команду на весь поток — это уже
# не команда, а группа.
MAX_TEAM_SIZE = 10
MAX_DURATION_MIN = 24 * 60

# Как судья называет ошибку компиляции. По ней посылка не штрафуется.
COMPILE_ERROR = "Compilation Error"


class ContestError(ValueError):
    """Действие не выполнить: сообщение уже переведено и годится для человека."""


# ------------------------------------------------------------------ настройка


def _check_numbers(team_size: int, duration_min: int, freeze_min: int) -> None:
    if not 1 <= team_size <= MAX_TEAM_SIZE:
        raise ContestError(_("В команде от 1 до %(max)s человек") % {"max": MAX_TEAM_SIZE})
    if not 1 <= duration_min <= MAX_DURATION_MIN:
        raise ContestError(_("Длительность — от минуты до суток"))
    if not 0 <= freeze_min < duration_min:
        raise ContestError(_("Заморозка должна быть короче соревнования"))


async def create(
    session: AsyncSession,
    author: User,
    *,
    title: str,
    rules: ContestRules = ContestRules.icpc,
    team_size: int = 3,
    duration_min: int = 300,
    freeze_min: int = 0,
    planned_at: datetime | None = None,
    description: str = "",
) -> Contest:
    """Новое соревнование — черновик: студенты его не видят до публикации."""
    title = title.strip()
    if not title:
        raise ContestError(_("Пустое название"))
    _check_numbers(team_size, duration_min, freeze_min)
    contest = Contest(
        title=title,
        description=description.strip() or None,
        rules=rules,
        team_size=team_size,
        duration_min=duration_min,
        freeze_min=freeze_min,
        planned_at=planned_at,
        created_by_id=author.id,
    )
    session.add(contest)
    await session.commit()
    # Новый объект не знает своих задач, а ленивой подгрузки в асинхронной
    # сессии нет — первое же `contest.problems` упало бы.
    await session.refresh(contest, ["problems"])
    return contest


async def update(
    session: AsyncSession,
    contest: Contest,
    *,
    title: str,
    rules: ContestRules,
    team_size: int,
    duration_min: int,
    freeze_min: int,
    planned_at: datetime | None,
    description: str = "",
) -> Contest:
    title = title.strip()
    if not title:
        raise ContestError(_("Пустое название"))
    _check_numbers(team_size, duration_min, freeze_min)
    if contest.started_at is not None:
        # Посреди тура правила и состав команд не меняют: табло за первый час
        # уже посчитано по одним, и пересчёт по другим никто не поймёт.
        if rules != contest.rules or team_size != contest.team_size:
            raise ContestError(_("После старта правила и размер команды не меняются"))
        if duration_min != contest.duration_min:
            raise ContestError(_("После старта время двигают кнопками «+время» и «Завершить»"))
    elif team_size < contest.team_size and await _largest_team(session, contest) > team_size:
        raise ContestError(_("Уже есть команды больше этого размера"))
    contest.title = title
    contest.description = description.strip() or None
    contest.rules = rules
    contest.team_size = team_size
    contest.duration_min = duration_min
    contest.freeze_min = freeze_min
    contest.planned_at = planned_at
    await session.commit()
    return contest


async def _largest_team(session: AsyncSession, contest: Contest) -> int:
    sizes = (
        select(func.count().label("n"))
        .select_from(ContestMember)
        .where(ContestMember.contest_id == contest.id)
        .group_by(ContestMember.team_id)
        .subquery()
    )
    return await session.scalar(select(func.max(sizes.c.n))) or 0


async def own_tasks(session: AsyncSession) -> list[Problem]:
    """Задачи, которые можно дать на соревнование: свои, с тестами на портале.

    Задачи с площадок не годятся: их проверяет площадка, и посылка команды
    на LeetCode до табло соревнования не доедет.
    """
    stmt = (
        select(Problem)
        .join(Task, Task.problem_id == Problem.id)
        .where(Problem.platform == Platform.local)
        .order_by(Problem.slug)
    )
    return list((await session.execute(stmt)).scalars().all())


async def set_problems(session: AsyncSession, contest: Contest, slugs: list[str]) -> list[Problem]:
    """Задачи соревнования по порядку: первая — A, вторая — B.

    Только до старта: буква задачи — это её имя на табло, и перестановка
    посреди тура сделала бы чужие «+» у B плюсами у C.
    """
    if contest.started_at is not None:
        raise ContestError(_("После старта задачи не меняются"))
    by_slug = {problem.slug: problem for problem in await own_tasks(session)}
    chosen: list[Problem] = []
    for raw in slugs:
        slug = raw.strip().removeprefix("task:").removeprefix("local:").strip()
        if not slug:
            continue
        problem = by_slug.get(slug)
        if problem is None:
            raise ContestError(_("Нет своей задачи «%(slug)s»") % {"slug": slug})
        if problem not in chosen:
            chosen.append(problem)
    contest.problems.clear()
    await session.flush()
    for position, problem in enumerate(chosen):
        contest.problems.append(ContestProblem(problem_id=problem.id, position=position))
    await session.commit()
    await session.refresh(contest, ["problems"])
    return chosen


async def slugs_of_set(session: AsyncSession, problem_set_id: int) -> list[str]:
    """Слаги своих задач из списка задач — чтобы не перепечатывать подобранное."""
    stmt = (
        select(Problem.slug)
        .join(ProblemSetItem, ProblemSetItem.problem_id == Problem.id)
        .join(Task, Task.problem_id == Problem.id)
        .where(ProblemSetItem.problem_set_id == problem_set_id)
        .order_by(ProblemSetItem.position, ProblemSetItem.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def publish(session: AsyncSession, contest: Contest, on: bool = True) -> None:
    if not on and contest.started_at is not None:
        raise ContestError(_("Начавшееся соревнование не спрятать"))
    contest.published = on
    await session.commit()


async def start(session: AsyncSession, contest: Contest, now: datetime | None = None) -> None:
    """Кнопка «Старт»: с этой секунды задачи видны и идёт время."""
    if contest.started_at is not None:
        raise ContestError(_("Соревнование уже идёт"))
    if not contest.problems:
        raise ContestError(_("Старт не прошёл: сначала задайте задачи"))
    now = now or utcnow()
    # Старт публикует: соревнование, которое идёт, а участники его не видят,
    # — это ошибка, а не настройка.
    contest.published = True
    contest.started_at = now
    contest.ends_at = now + timedelta(minutes=contest.duration_min)
    await session.commit()


async def extend(session: AsyncSession, contest: Contest, minutes: int) -> None:
    """«+время»: сдвигает конец. Можно и после конца — продолжить тур."""
    if contest.started_at is None or contest.ends_at is None:
        raise ContestError(_("Соревнование ещё не началось"))
    if not 1 <= minutes <= MAX_DURATION_MIN:
        raise ContestError(_("Добавить можно от минуты до суток"))
    base = max(contest.ends_at, utcnow())
    contest.ends_at = base + timedelta(minutes=minutes)
    await session.commit()


async def finish(session: AsyncSession, contest: Contest, now: datetime | None = None) -> None:
    """«Завершить»: конец сейчас, табло размораживается."""
    if contest.started_at is None:
        raise ContestError(_("Соревнование ещё не началось"))
    now = now or utcnow()
    if contest.ends_at is None or contest.ends_at > now:
        contest.ends_at = now
    await session.commit()


# ------------------------------------------------------------------ видимость


async def visible(session: AsyncSession, user: User) -> list[Contest]:
    """Соревнования, которые человек видит в списке: черновики — только
    преподавателю."""
    stmt = select(Contest).order_by(Contest.created_at.desc())
    if not user.is_teacher:
        stmt = stmt.where(Contest.published.is_(True))
    return list((await session.execute(stmt)).scalars().all())


def may_see(contest: Contest, user: User) -> bool:
    return user.is_teacher or contest.published


def problems_open(contest: Contest, user: User, now: datetime | None = None) -> bool:
    """Условия видны после старта. До него задач нет — ни списком, ни по адресу."""
    if user.is_teacher:
        return True
    return contest.phase(now) in ("running", "finished")


def problem_by_letter(contest: Contest, letter: str) -> ContestProblem | None:
    letter = letter.upper()
    return next((item for item in contest.problems if item.letter == letter), None)


async def in_contest(session: AsyncSession, problem_id: int) -> bool:
    """Задача соревнования вне раздела не открывается вовсе.

    До старта — потому что слаг из архива легко угадать. Во время тура —
    потому что посылка мимо раздела не попала бы в табло. После конца —
    дорешивание живёт в разделе, а задача не становится учебной сама.
    """
    return (
        await session.scalar(
            select(ContestProblem.id).where(ContestProblem.problem_id == problem_id).limit(1)
        )
    ) is not None


# ------------------------------------------------------------------ команды


async def team_of(session: AsyncSession, contest: Contest, user: User) -> ContestTeam | None:
    return await session.scalar(
        select(ContestTeam)
        .join(ContestMember, ContestMember.team_id == ContestTeam.id)
        .where(ContestMember.contest_id == contest.id, ContestMember.user_id == user.id)
    )


async def teams(session: AsyncSession, contest: Contest) -> list[ContestTeam]:
    stmt = (
        select(ContestTeam)
        .where(ContestTeam.contest_id == contest.id)
        .order_by(ContestTeam.created_at)
    )
    return list((await session.execute(stmt)).scalars().all())


def _registration_open(contest: Contest) -> None:
    # Опоздавший на полчаса всё равно должен сесть и решать — поэтому
    # регистрация открыта и во время тура. После конца — дорешивание, команда
    # ему уже не нужна.
    if not contest.published:
        raise ContestError(_("Соревнование ещё не опубликовано"))
    if contest.phase() == "finished":
        raise ContestError(_("Соревнование закончилось"))


async def _unique_name(session: AsyncSession, contest: Contest, name: str) -> str:
    taken = set(
        (
            await session.execute(
                select(ContestTeam.name).where(ContestTeam.contest_id == contest.id)
            )
        ).scalars()
    )
    if name not in taken:
        return name
    number = 2
    while f"{name} ({number})" in taken:
        number += 1
    return f"{name} ({number})"


async def create_team(
    session: AsyncSession, contest: Contest, user: User, name: str = ""
) -> ContestTeam:
    """Капитан заводит команду. В личном соревновании это и есть регистрация:
    команда из одного, названная по имени участника."""
    _registration_open(contest)
    if await team_of(session, contest, user) is not None:
        raise ContestError(_("Ты уже в команде этого соревнования"))
    if contest.is_team:
        name = name.strip()[:80]
        if not name:
            raise ContestError(_("Пустое название команды"))
        exists = await session.scalar(
            select(ContestTeam.id).where(
                ContestTeam.contest_id == contest.id, ContestTeam.name == name
            )
        )
        if exists:
            raise ContestError(_("Команда с таким названием уже есть"))
    else:
        name = await _unique_name(session, contest, user.display_name[:70])
    team = ContestTeam(
        contest_id=contest.id,
        name=name,
        captain_id=user.id,
        join_code=secrets.token_hex(4),
    )
    team.members.append(ContestMember(contest_id=contest.id, user_id=user.id))
    session.add(team)
    await session.commit()
    return team


async def join_team(session: AsyncSession, contest: Contest, user: User, code: str) -> ContestTeam:
    _registration_open(contest)
    if not contest.is_team:
        raise ContestError(_("Соревнование личное — команды в нём из одного"))
    if await team_of(session, contest, user) is not None:
        raise ContestError(_("Ты уже в команде этого соревнования"))
    team = await session.scalar(
        select(ContestTeam).where(
            ContestTeam.contest_id == contest.id, ContestTeam.join_code == code.strip().lower()
        )
    )
    if team is None:
        raise ContestError(_("Код не подошёл — проверь его у капитана"))
    if len(team.members) >= contest.team_size:
        raise ContestError(_("Команда уже полная"))
    team.members.append(ContestMember(contest_id=contest.id, user_id=user.id))
    await session.commit()
    return team


async def leave_team(session: AsyncSession, contest: Contest, user: User) -> None:
    """Выйти из команды до старта. Последний вышедший распускает команду,
    ушедший капитан передаёт роль тому, кто пришёл раньше остальных."""
    if contest.started_at is not None:
        raise ContestError(_("После старта состав команды меняет преподаватель"))
    team = await team_of(session, contest, user)
    if team is None:
        return
    await _drop_member(session, team, user.id)


async def remove_member(session: AsyncSession, team: ContestTeam, user_id: int) -> None:
    """Преподаватель убирает человека из команды — в любой момент."""
    await _drop_member(session, team, user_id)


async def _drop_member(session: AsyncSession, team: ContestTeam, user_id: int) -> None:
    member = next((m for m in team.members if m.user_id == user_id), None)
    if member is None:
        return
    team.members.remove(member)
    await session.flush()
    if not team.members:
        await session.delete(team)
    elif team.captain_id == user_id:
        team.captain_id = team.members[0].user_id
    await session.commit()


async def delete_team(session: AsyncSession, team: ContestTeam) -> None:
    await session.delete(team)
    await session.commit()


# ------------------------------------------------------------------ посылки


async def record_run(
    session: AsyncSession,
    contest: Contest,
    *,
    user: User,
    team: ContestTeam | None,
    problem: Problem,
    code: str,
    language: str,
    result,
) -> ContestRun:
    """Записывает посылку. `result` — ответ судьи или None, если судья не
    подключён: тогда посылка сохраняется без вердикта и ни на что не влияет.

    Дорешивание — всё, что отправлено не участником идущего тура: после конца,
    без команды или преподавателем.
    """
    now = utcnow()
    practice = user.is_teacher or team is None or contest.phase(now) != "running"
    if result is None:
        verdict, accepted, compile_error, passed, total = _("Не проверено"), False, False, 0, 0
    else:
        passed = sum(1 for r in result.results if r.passed)
        total = len(result.results)
        accepted = result.passed
        compile_error = bool(result.results) and result.results[0].status == COMPILE_ERROR
        verdict = result.summary
    run = ContestRun(
        contest_id=contest.id,
        team_id=team.id if team else None,
        user_id=user.id,
        problem_id=problem.id,
        language=language,
        code=code,
        verdict=verdict[:80],
        accepted=accepted,
        compile_error=compile_error,
        passed_tests=passed,
        total_tests=total,
        practice=practice,
        submitted_at=now,
    )
    session.add(run)
    await session.commit()
    return run


async def runs(
    session: AsyncSession,
    contest: Contest,
    *,
    team: ContestTeam | None = None,
    user: User | None = None,
    problem_id: int | None = None,
) -> list[ContestRun]:
    """Посылки соревнования, свежие сверху. Без фильтров — все (для преподавателя)."""
    stmt = select(ContestRun).where(ContestRun.contest_id == contest.id)
    if team is not None and user is not None:
        # Своя команда плюс собственное дорешивание без команды.
        stmt = stmt.where((ContestRun.team_id == team.id) | (ContestRun.user_id == user.id))
    elif team is not None:
        stmt = stmt.where(ContestRun.team_id == team.id)
    elif user is not None:
        stmt = stmt.where(ContestRun.user_id == user.id)
    if problem_id is not None:
        stmt = stmt.where(ContestRun.problem_id == problem_id)
    stmt = stmt.order_by(ContestRun.submitted_at.desc(), ContestRun.id.desc())
    return list((await session.execute(stmt)).scalars().all())


async def may_read_run(
    session: AsyncSession, contest: Contest, run: ContestRun, user: User
) -> bool:
    """Код посылки видят её команда и преподаватель. Чужой код посреди тура —
    это списывание, а не открытость."""
    if user.is_teacher or run.user_id == user.id:
        return True
    team = await team_of(session, contest, user)
    return team is not None and run.team_id == team.id


# ------------------------------------------------------------------ табло


@dataclass(slots=True)
class Cell:
    solved: bool = False
    # Отклонённые посылки до принятой (ICPC) или все посылки (IOI).
    tries: int = 0
    # Минута от старта, на которой задача принята.
    minute: int | None = None
    # Посылки в заморозку: их результат участникам не показывается.
    pending: int = 0
    score: float = 0.0
    first: bool = False

    @property
    def touched(self) -> bool:
        return self.solved or self.tries > 0 or self.pending > 0 or self.score > 0


@dataclass(slots=True)
class Row:
    team: ContestTeam
    cells: dict[int, Cell] = field(default_factory=dict)
    solved: int = 0
    penalty: int = 0
    score: float = 0.0
    # Минута последнего улучшения — разбивает ничьи по баллам IOI.
    last_minute: int = 0
    place: int = 0

    def cell(self, problem_id: int) -> Cell:
        return self.cells.get(problem_id) or Cell()


@dataclass(slots=True)
class Standings:
    contest: Contest
    problems: list[ContestProblem]
    rows: list[Row]
    frozen: bool

    def solved_by(self, problem_id: int) -> int:
        return sum(1 for row in self.rows if row.cell(problem_id).solved)

    def tried_by(self, problem_id: int) -> int:
        return sum(1 for row in self.rows if row.cell(problem_id).touched)


def _minute(contest: Contest, moment: datetime) -> int:
    assert contest.started_at is not None
    return max(0, int((moment - contest.started_at).total_seconds() // 60))


async def standings(
    session: AsyncSession, contest: Contest, *, live: bool = False, now: datetime | None = None
) -> Standings:
    """Табло. `live` — без заморозки: его видит преподаватель.

    В заморозку участники видят чужие (и свои) посылки последних минут
    знаком вопроса: что задача пробовалась, но не чем кончилось. Иначе
    заморозка ничего не прячет.
    """
    now = now or utcnow()
    team_list = await teams(session, contest)
    rows = {team.id: Row(team=team) for team in team_list}
    problems = list(contest.problems)
    frozen = not live and contest.frozen(now)
    freeze_at = (
        contest.ends_at - timedelta(minutes=contest.freeze_min)
        if frozen and contest.ends_at is not None
        else None
    )

    if contest.started_at is not None:
        stmt = (
            select(ContestRun)
            .where(
                ContestRun.contest_id == contest.id,
                ContestRun.practice.is_(False),
                ContestRun.team_id.is_not(None),
            )
            .order_by(ContestRun.submitted_at, ContestRun.id)
        )
        for run in (await session.execute(stmt)).scalars():
            row = rows.get(run.team_id)
            if row is None:
                continue
            cell = row.cells.setdefault(run.problem_id, Cell())
            if freeze_at is not None and run.submitted_at >= freeze_at:
                if not cell.solved:
                    cell.pending += 1
                continue
            if contest.rules == ContestRules.icpc:
                _icpc_apply(contest, cell, run)
            else:
                _ioi_apply(contest, cell, run)

    for row in rows.values():
        for cell in row.cells.values():
            if cell.solved:
                row.solved += 1
                if contest.rules == ContestRules.icpc:
                    row.penalty += (cell.minute or 0) + PENALTY_MIN * cell.tries
            row.score += cell.score
        row.score = round(row.score, 2)
        row.last_minute = max(
            (c.minute or 0 for c in row.cells.values() if c.score > 0), default=0
        )

    ordered = sorted(rows.values(), key=lambda r: (_key(contest, r), r.team.name.lower()))
    for index, row in enumerate(ordered):
        same = index and _key(contest, ordered[index - 1]) == _key(contest, row)
        row.place = ordered[index - 1].place if same else index + 1

    for item in problems:
        firsts = [
            (cell.minute, row.team.id)
            for row in ordered
            if (cell := row.cell(item.problem_id)).solved and cell.minute is not None
        ]
        if firsts:
            first_minute = min(firsts)[0]
            for row in ordered:
                cell = row.cells.get(item.problem_id)
                if cell and cell.solved and cell.minute == first_minute:
                    cell.first = True

    return Standings(contest=contest, problems=problems, rows=ordered, frozen=frozen)


def _icpc_apply(contest: Contest, cell: Cell, run: ContestRun) -> None:
    if cell.solved or run.compile_error or not run.total_tests:
        return
    if run.accepted:
        cell.solved = True
        cell.minute = _minute(contest, run.submitted_at)
    else:
        cell.tries += 1


def _ioi_apply(contest: Contest, cell: Cell, run: ContestRun) -> None:
    if not run.total_tests:
        return
    cell.tries += 1
    if run.score > cell.score:
        cell.score = run.score
        cell.minute = _minute(contest, run.submitted_at)
    if run.accepted and not cell.solved:
        cell.solved = True


def _key(contest: Contest, row: Row) -> tuple:
    if contest.rules == ContestRules.icpc:
        return (-row.solved, row.penalty)
    return (-row.score, row.last_minute)
