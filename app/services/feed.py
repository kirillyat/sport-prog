"""Лента решений: кто из группы что решил и когда.

Событием считается ПЕРВОЕ принятое решение задачи конкретным студентом —
иначе перерешивания и повторные отправки засоряют ленту.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Assignment,
    Group,
    GroupMembership,
    Platform,
    Problem,
    ProblemSetItem,
    Submission,
    User,
)
from app.platforms.codeforces import submission_url as codeforces_submission_url

DEFAULT_LIMIT = 60


@dataclass(slots=True)
class FeedItem:
    user: User
    platform: Platform
    title: str
    solved_at: datetime
    problem: Problem | None = None
    assignment: Assignment | None = None
    submission: Submission | None = None

    @property
    def url(self) -> str | None:
        return self.problem.url if self.problem else None

    @property
    def submission_url(self) -> str | None:
        """Где посмотреть исходник решения.

        У Codeforces страница посылки публичная. У LeetCode такой страницы нет
        вообще: `/submissions/detail/` открывается только автору, а код лежит
        за запросом, которому нужна его сессия. Поэтому для LeetCode — None,
        и лучше ничего не показать, чем показать ссылку в никуда.
        """
        if self.submission is None or self.platform != Platform.codeforces:
            return None
        return codeforces_submission_url(self.submission.problem_slug, self.submission.external_id)

    @property
    def difficulty_badge(self) -> tuple[str, str] | None:
        """(текст, css-класс) или None, если сложность неизвестна."""
        if self.problem is None:
            return None
        if self.problem.difficulty:
            return self.problem.difficulty, self.problem.difficulty.lower()
        if self.problem.rating:
            return str(self.problem.rating), "rating"
        return None


async def visible_user_ids(
    session: AsyncSession, viewer: User, group_id: int | None = None
) -> list[int]:
    """Чьи решения показываем: преподавателю — всех, студенту — только одногруппников."""
    if viewer.is_teacher:
        stmt = select(User.id).where(User.is_active.is_(True))
        if group_id is not None:
            stmt = stmt.join(GroupMembership, GroupMembership.user_id == User.id).where(
                GroupMembership.group_id == group_id
            )
        return list((await session.execute(stmt)).scalars().all())

    my_groups = select(GroupMembership.group_id).where(GroupMembership.user_id == viewer.id)
    if group_id is not None:
        my_groups = my_groups.where(GroupMembership.group_id == group_id)
    stmt = (
        select(GroupMembership.user_id)
        .join(User, User.id == GroupMembership.user_id)
        .where(GroupMembership.group_id.in_(my_groups), User.is_active.is_(True))
        .distinct()
    )
    ids = set((await session.execute(stmt)).scalars().all())
    ids.add(viewer.id)
    return sorted(ids)


async def _assignment_index(
    session: AsyncSession, user_ids: list[int]
) -> dict[int, list[tuple[Assignment, set[int]]]]:
    """user_id -> список (задание, множество id задач), чтобы подписать событие заданием."""
    if not user_ids:
        return {}

    memberships = (
        await session.execute(
            select(GroupMembership.user_id, GroupMembership.group_id).where(
                GroupMembership.user_id.in_(user_ids)
            )
        )
    ).all()
    groups_of: dict[int, set[int]] = {}
    for user_id, group_id in memberships:
        groups_of.setdefault(user_id, set()).add(group_id)

    assignments = list((await session.execute(select(Assignment))).scalars().all())
    if not assignments:
        return {}

    items = (
        await session.execute(
            select(ProblemSetItem.problem_set_id, ProblemSetItem.problem_id).where(
                ProblemSetItem.problem_set_id.in_({a.problem_set_id for a in assignments})
            )
        )
    ).all()
    problems_of_set: dict[int, set[int]] = {}
    for set_id, problem_id in items:
        problems_of_set.setdefault(set_id, set()).add(problem_id)

    index: dict[int, list[tuple[Assignment, set[int]]]] = {}
    for assignment in assignments:
        problem_ids = problems_of_set.get(assignment.problem_set_id, set())
        if not problem_ids:
            continue
        for user_id in user_ids:
            targets = assignment.user_id == user_id or (
                assignment.group_id is not None
                and assignment.group_id in groups_of.get(user_id, set())
            )
            if targets:
                index.setdefault(user_id, []).append((assignment, problem_ids))
    return index


def _match_assignment(
    index: dict[int, list[tuple[Assignment, set[int]]]],
    user_id: int,
    problem_id: int | None,
    solved_at: datetime,
) -> Assignment | None:
    if problem_id is None:
        return None
    candidates = [
        assignment
        for assignment, problem_ids in index.get(user_id, [])
        if problem_id in problem_ids and assignment.assigned_at <= solved_at
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda a: a.assigned_at)


async def build_feed(
    session: AsyncSession,
    viewer: User,
    *,
    group_id: int | None = None,
    only_user: User | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[FeedItem]:
    if only_user is not None:
        # Лента одного человека для страницы профиля.
        user_ids = [only_user.id]
    else:
        user_ids = await visible_user_ids(session, viewer, group_id)
    if not user_ids:
        return []

    # Ключ события — задача, а не посылка: у одной задачи бывает много AC.
    key = func.coalesce(Submission.problem_slug, Submission.external_id)
    stmt = (
        select(
            Submission.user_id,
            Submission.platform,
            func.min(Submission.submitted_at).label("solved_at"),
            func.max(Submission.problem_id).label("problem_id"),
            func.max(Submission.problem_title).label("title"),
            # Своя первичка, а не внешний номер: max() по строке дал бы чепуху.
            func.max(Submission.id).label("submission_row_id"),
        )
        .where(Submission.is_accepted.is_(True), Submission.user_id.in_(user_ids))
        .group_by(Submission.user_id, Submission.platform, key)
        .order_by(func.min(Submission.submitted_at).desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    if not rows:
        return []

    users = {
        u.id: u
        for u in (
            await session.execute(select(User).where(User.id.in_({r.user_id for r in rows})))
        ).scalars()
    }
    problem_ids = {r.problem_id for r in rows if r.problem_id}
    problems = {
        p.id: p
        for p in (
            await session.execute(select(Problem).where(Problem.id.in_(problem_ids)))
        ).scalars()
    }
    submissions = {
        s.id: s
        for s in (
            await session.execute(
                select(Submission).where(
                    Submission.id.in_({r.submission_row_id for r in rows if r.submission_row_id})
                )
            )
        ).scalars()
    }
    index = await _assignment_index(session, list(users))

    feed: list[FeedItem] = []
    for row in rows:
        user = users.get(row.user_id)
        if user is None:
            continue
        problem = problems.get(row.problem_id) if row.problem_id else None
        feed.append(
            FeedItem(
                user=user,
                platform=Platform(row.platform),
                title=(problem.title if problem else row.title) or "задача",
                solved_at=row.solved_at,
                problem=problem,
                assignment=_match_assignment(index, row.user_id, row.problem_id, row.solved_at),
                submission=submissions.get(row.submission_row_id),
            )
        )
    return feed


async def groups_for_feed(session: AsyncSession, viewer: User) -> list[Group]:
    stmt = select(Group).where(Group.is_archived.is_(False))
    if not viewer.is_teacher:
        stmt = stmt.join(GroupMembership, GroupMembership.group_id == Group.id).where(
            GroupMembership.user_id == viewer.id
        )
    return list((await session.execute(stmt.order_by(Group.title))).scalars().all())
