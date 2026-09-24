"""Разбор списка задач, вставленного преподавателем текстом.

Принимает вперемешку ссылки, слаги и коды задач — по строке на задачу:

    https://leetcode.com/problems/two-sum/
    two-sum
    lc:binary-search
    https://codeforces.com/problemset/problem/4/A
    cf:4A
    1352G
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Platform, Problem

LC_URL = re.compile(r"leetcode\.com/problems/([a-z0-9-]+)", re.I)
CF_URL = re.compile(
    r"codeforces\.com/(?:contest|problemset/problem)/(\d+)/?(?:problem/)?([A-Za-z]\d*)",
    re.I,
)
CF_CODE = re.compile(r"^(\d{1,6})([A-Za-z]\d*)$")
LC_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(slots=True)
class ParseResult:
    problems: list[Problem] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _Ref:
    platform: Platform | None
    key: str
    raw: str


def _classify(line: str) -> _Ref | None:
    line = line.strip().strip(",;")
    if not line:
        return None

    if match := LC_URL.search(line):
        return _Ref(Platform.leetcode, match.group(1).lower(), line)
    if match := CF_URL.search(line):
        return _Ref(Platform.codeforces, f"{match.group(1)}{match.group(2).upper()}", line)

    lowered = line.lower()
    # Свои задачи пишутся с приставкой: голый слаг неотличим от LeetCode,
    # а угадывать, какую из двух задач имели в виду, — плохая идея.
    for prefix in ("task:", "local:"):
        if lowered.startswith(prefix):
            return _Ref(Platform.local, line[len(prefix) :].strip().lower(), line)
    for prefix, platform in (("lc:", Platform.leetcode), ("cf:", Platform.codeforces)):
        if lowered.startswith(prefix):
            rest = line[len(prefix) :].strip()
            if platform == Platform.codeforces:
                match = CF_CODE.match(rest)
                if match:
                    return _Ref(platform, f"{match.group(1)}{match.group(2).upper()}", line)
                return _Ref(platform, rest, line)
            return _Ref(platform, rest.lower(), line)

    if match := CF_CODE.match(line):
        return _Ref(Platform.codeforces, f"{match.group(1)}{match.group(2).upper()}", line)
    if LC_SLUG.match(lowered):
        return _Ref(Platform.leetcode, lowered, line)
    return _Ref(None, line, line)


async def parse_problem_list(session: AsyncSession, text: str) -> ParseResult:
    refs = [ref for ref in (_classify(line) for line in text.splitlines()) if ref is not None]
    result = ParseResult()
    if not refs:
        return result

    cf_keys = {r.key for r in refs if r.platform == Platform.codeforces}
    lc_keys = {r.key for r in refs if r.platform == Platform.leetcode}
    local_keys = {r.key for r in refs if r.platform == Platform.local}

    found: dict[tuple[Platform, str], Problem] = {}
    if cf_keys:
        rows = await session.execute(
            select(Problem).where(
                Problem.platform == Platform.codeforces,
                func.upper(Problem.external_id).in_({k.upper() for k in cf_keys}),
            )
        )
        for problem in rows.scalars():
            found[(Platform.codeforces, problem.external_id.upper())] = problem
    if lc_keys:
        rows = await session.execute(
            select(Problem).where(Problem.platform == Platform.leetcode, Problem.slug.in_(lc_keys))
        )
        for problem in rows.scalars():
            found[(Platform.leetcode, problem.slug)] = problem
    if local_keys:
        rows = await session.execute(
            select(Problem).where(Problem.platform == Platform.local, Problem.slug.in_(local_keys))
        )
        for problem in rows.scalars():
            found[(Platform.local, problem.slug)] = problem

    seen: set[int] = set()
    for ref in refs:
        problem = None
        if ref.platform == Platform.codeforces:
            problem = found.get((Platform.codeforces, ref.key.upper()))
        elif ref.platform == Platform.leetcode:
            problem = found.get((Platform.leetcode, ref.key))
        elif ref.platform == Platform.local:
            problem = found.get((Platform.local, ref.key))
        if problem is None:
            result.unresolved.append(ref.raw)
        elif problem.id not in seen:
            seen.add(problem.id)
            result.problems.append(problem)
    return result


async def search_problems(
    session: AsyncSession,
    query: str,
    platform: Platform | None = None,
    limit: int = 40,
) -> list[Problem]:
    stmt = select(Problem)
    if platform:
        stmt = stmt.where(Problem.platform == platform)
    query = query.strip()
    if query:
        pattern = f"%{query.lower()}%"
        stmt = stmt.where(
            func.lower(Problem.title).like(pattern)
            | func.lower(Problem.slug).like(pattern)
            | func.lower(Problem.external_id).like(pattern)
        )
    stmt = stmt.order_by(Problem.platform, Problem.title).limit(limit)
    return list((await session.execute(stmt)).scalars().all())
