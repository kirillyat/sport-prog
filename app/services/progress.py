from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import Select, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.access import confirmed_clause
from app.models import (
    Assignment,
    Group,
    GroupMembership,
    Problem,
    ProblemSetItem,
    ReviewStatus,
    Role,
    SolutionUpload,
    SolveStatus,
    Submission,
    User,
)

# В зачёт идёт только решённое после выдачи и до дедлайна. Опоздание видно
# значком, но веса не имеет: иначе «до дедлайна» перестаёт что-либо значить.
SOLVED_STATUSES = {SolveStatus.solved_in_time}


@dataclass(slots=True)
class Cell:
    status: SolveStatus = SolveStatus.not_solved
    solved_at: datetime | None = None
    first_ever_at: datetime | None = None
    # Проверка присланного кода. None — код не прислан.
    review: ReviewStatus | None = None
    # Задание требует прислать код. Без него зачёта нет, даже если задача
    # решена на площадке: проверять преподавателю было бы нечего.
    needs_code: bool = False

    @property
    def rejected(self) -> bool:
        return self.review == ReviewStatus.rejected

    @property
    def code_missing(self) -> bool:
        return self.needs_code and self.review is None

    @property
    def counts(self) -> bool:
        """Засчитывается ли решение в прогресс по заданию.

        Три условия: решено в срок, код прислан (если его просят) и не отклонён.
        Код на проверке зачёт даёт — студент своё сделал, снимает его только
        отклонение.
        """
        if self.status not in SOLVED_STATUSES:
            return False
        return not self.rejected and not self.code_missing

    @property
    def awaiting_code(self) -> bool:
        """Решено в срок, а зачёта нет только потому, что код не прислан.

        Отдельно от `code_missing`: там, где задача и не решена, требование
        кода ничего не меняет, и красить клетку нечем.
        """
        return self.status == SolveStatus.solved_in_time and self.code_missing


@dataclass(slots=True)
class AssignmentProgress:
    assignment: Assignment
    problems: list[Problem]
    participants: list[User]
    cells: dict[tuple[int, int], Cell] = field(default_factory=dict)

    def cell(self, user_id: int, problem_id: int) -> Cell:
        return self.cells.get((user_id, problem_id), Cell())

    def row(self, user_id: int) -> list[Cell]:
        return [self.cell(user_id, p.id) for p in self.problems]

    def solved_count(self, user_id: int) -> int:
        return sum(1 for c in self.row(user_id) if c.counts)

    def problem_solved_count(self, problem_id: int) -> int:
        return sum(1 for u in self.participants if self.cell(u.id, problem_id).counts)

    @property
    def total_problems(self) -> int:
        return len(self.problems)

    def first_solver(self, problem_id: int) -> User | None:
        """Кто закрыл задачу раньше всех. В одиночном задании соревноваться не с кем."""
        if len(self.participants) < 2:
            return None
        solved = [
            (cell.solved_at, user)
            for user in self.participants
            if (cell := self.cell(user.id, problem_id)).counts and cell.solved_at is not None
        ]
        return min(solved, key=lambda pair: pair[0])[1] if solved else None

    def is_complete(self, user_id: int) -> bool:
        return self.total_problems > 0 and self.solved_count(user_id) == self.total_problems


async def participants_for_assignment(session: AsyncSession, assignment: Assignment) -> list[User]:
    if assignment.user_id is not None:
        user = await session.get(User, assignment.user_id)
        return [user] if user else []
    stmt: Select = select(User).where(User.is_active.is_(True))
    if assignment.group_id is not None:
        stmt = stmt.join(GroupMembership, GroupMembership.user_id == User.id).where(
            GroupMembership.group_id == assignment.group_id
        )
    else:
        # Задание всем: студенты, подтвердившие вуз. Преподаватель его выдал,
        # а не получил, и в матрице ему делать нечего.
        stmt = stmt.where(User.role != Role.teacher, confirmed_clause())
    return list((await session.execute(stmt.order_by(User.display_name))).scalars().all())


async def problems_for_set(session: AsyncSession, problem_set_id: int) -> list[Problem]:
    stmt = (
        select(Problem)
        .join(ProblemSetItem, ProblemSetItem.problem_id == Problem.id)
        .where(ProblemSetItem.problem_set_id == problem_set_id)
        .order_by(ProblemSetItem.position, ProblemSetItem.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _solve_times(
    session: AsyncSession, user_ids: list[int], problem_ids: list[int], since: datetime
) -> dict[tuple[int, int], tuple[datetime | None, datetime | None]]:
    """(user, problem) -> (первое решение вообще, первое решение после `since`)."""
    if not user_ids or not problem_ids:
        return {}
    after = func.min(case((Submission.submitted_at >= since, Submission.submitted_at)))
    stmt = (
        select(Submission.user_id, Submission.problem_id, func.min(Submission.submitted_at), after)
        .where(
            Submission.is_accepted.is_(True),
            Submission.user_id.in_(user_ids),
            Submission.problem_id.in_(problem_ids),
        )
        .group_by(Submission.user_id, Submission.problem_id)
    )
    out: dict[tuple[int, int], tuple[datetime | None, datetime | None]] = {}
    for user_id, problem_id, first_ever, first_after in (await session.execute(stmt)).all():
        out[(user_id, problem_id)] = (first_ever, first_after)
    return out


def _status(
    first_ever: datetime | None,
    first_after: datetime | None,
    deadline: datetime | None,
    count_prior: bool,
) -> tuple[SolveStatus, datetime | None]:
    if first_after is not None:
        if deadline is None or first_after <= deadline:
            return SolveStatus.solved_in_time, first_after
        # После дедлайна решение видно, но в зачёт не идёт — одинаково у всех
        # заданий. Отдельного «жёсткого» дедлайна больше нет: пока опоздание
        # засчитывалось наполовину, разница была, теперь её нет.
        return SolveStatus.solved_late, first_after
    if first_ever is not None:
        # Задача была решена ещё до выдачи задания. По умолчанию не засчитываем,
        # иначе баллы капают за работу прошлых лет.
        if count_prior:
            return SolveStatus.solved_in_time, first_ever
        return SolveStatus.solved_before, first_ever
    return SolveStatus.not_solved, None


async def compute_progress(
    session: AsyncSession,
    assignment: Assignment,
    participants: list[User] | None = None,
) -> AssignmentProgress:
    participants = (
        participants
        if participants is not None
        else await participants_for_assignment(session, assignment)
    )
    problems = await problems_for_set(session, assignment.problem_set_id)
    progress = AssignmentProgress(
        assignment=assignment, problems=problems, participants=participants
    )

    times = await _solve_times(
        session,
        [u.id for u in participants],
        [p.id for p in problems],
        assignment.assigned_at,
    )
    reviews: dict[tuple[int, int], ReviewStatus] = {}
    if assignment.requires_solution:
        stmt = select(
            SolutionUpload.user_id, SolutionUpload.problem_id, SolutionUpload.status
        ).where(SolutionUpload.assignment_id == assignment.id)
        for user_id, problem_id, status in (await session.execute(stmt)).all():
            reviews[(user_id, problem_id)] = status

    for (user_id, problem_id), (first_ever, first_after) in times.items():
        status, solved_at = _status(
            first_ever,
            first_after,
            assignment.deadline,
            assignment.count_prior_solves,
        )
        progress.cells[(user_id, problem_id)] = Cell(
            status=status,
            solved_at=solved_at,
            first_ever_at=first_ever,
            review=reviews.get((user_id, problem_id)),
            needs_code=assignment.requires_solution,
        )
    return progress


async def assignments_for_user(session: AsyncSession, user: User) -> list[Assignment]:
    group_ids = select(GroupMembership.group_id).where(GroupMembership.user_id == user.id)
    stmt = (
        select(Assignment)
        .where(
            (Assignment.user_id == user.id)
            | (Assignment.group_id.in_(group_ids))
            # Задание всем: обе ссылки пусты.
            | ((Assignment.group_id.is_(None)) & (Assignment.user_id.is_(None))),
        )
        .order_by(Assignment.assigned_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def groups_for_user(session: AsyncSession, user: User) -> list[Group]:
    stmt = (
        select(Group)
        .join(GroupMembership, GroupMembership.group_id == Group.id)
        .where(GroupMembership.user_id == user.id, Group.is_archived.is_(False))
        .order_by(Group.title)
    )
    return list((await session.execute(stmt)).scalars().all())
