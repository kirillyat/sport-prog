"""Списки задач: создание и пополнение.

Одно место и для формы преподавателя, и для нейросети по MCP: если правило
поменяется (скажем, повторы станут запрещены), оно поменяется у обоих.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Assignment, Problem, ProblemSet, ProblemSetItem, User
from app.services.problem_parser import ParseResult, parse_problem_list


async def create(
    session: AsyncSession, author: User, title: str, description: str, problems_text: str
) -> tuple[ProblemSet, ParseResult]:
    """Новый список. Нераспознанные строки не мешают создать список из остальных."""
    problem_set = ProblemSet(
        title=title.strip(), description=description.strip() or None, created_by_id=author.id
    )
    session.add(problem_set)
    await session.commit()
    await session.refresh(problem_set)

    result = ParseResult()
    if problems_text.strip():
        result = await parse_problem_list(session, problems_text)
        for position, problem in enumerate(result.problems):
            session.add(
                ProblemSetItem(
                    problem_set_id=problem_set.id, problem_id=problem.id, position=position
                )
            )
        await session.commit()
    return problem_set, result


async def add(
    session: AsyncSession, problem_set: ProblemSet, problems_text: str
) -> tuple[list[Problem], list[str]]:
    """Дописывает задачи в конец. Уже стоящие в списке пропускаются молча."""
    existing = {item.problem_id for item in problem_set.items}
    next_position = max((item.position for item in problem_set.items), default=-1) + 1

    result = await parse_problem_list(session, problems_text)
    added: list[Problem] = []
    for problem in result.problems:
        if problem.id in existing:
            continue
        session.add(
            ProblemSetItem(
                problem_set_id=problem_set.id,
                problem_id=problem.id,
                position=next_position + len(added),
            )
        )
        added.append(problem)
    await session.commit()
    return added, result.unresolved


async def used_in(
    session: AsyncSession, problem_ids: list[int]
) -> dict[int, list[tuple[ProblemSet, list[Assignment]]]]:
    """Где задача уже стоит: списки и задания по ним.

    Нужно до создания списка: задача, которую группа решала месяц назад,
    на контрольной ничего не проверит.
    """
    if not problem_ids:
        return {}
    rows = (
        await session.execute(
            select(ProblemSetItem.problem_id, ProblemSet)
            .join(ProblemSet, ProblemSet.id == ProblemSetItem.problem_set_id)
            .where(ProblemSetItem.problem_id.in_(problem_ids))
            .order_by(ProblemSet.created_at)
        )
    ).all()
    set_ids = {problem_set.id for _, problem_set in rows}
    assignments: dict[int, list[Assignment]] = {}
    if set_ids:
        for assignment in (
            await session.execute(
                select(Assignment)
                .where(Assignment.problem_set_id.in_(set_ids))
                .order_by(Assignment.assigned_at)
            )
        ).scalars():
            assignments.setdefault(assignment.problem_set_id, []).append(assignment)

    out: dict[int, list[tuple[ProblemSet, list[Assignment]]]] = {}
    for problem_id, problem_set in rows:
        out.setdefault(problem_id, []).append(
            (problem_set, assignments.get(problem_set.id, []))
        )
    return out
