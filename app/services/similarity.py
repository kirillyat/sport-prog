"""Похожие решения одной задачи — повод посмотреть, а не приговор.

Сравниваем не текст, а его скелет: комментарии выброшены, строки и числа
заменены метками, имена переменных — одной общей меткой. Переименовать
переменные и переставить пробелы — самый частый способ «сделать своим»,
а скелет от этого не меняется. Ключевые слова, встроенные функции и методы
остаются как есть: они и есть устройство решения.

Скелет режем на кусочки по K подряд идущих элементов; похожесть — доля
общих кусочков у двух решений (мера Жаккара). Переставленные функции и
лишняя строка почти ничего не меняют, а разные решения одной задачи дают
обычно 10–40 %.

Отдельная улика — общие редкие имена: имя, которое из всей группы есть
только у этих двоих, говорит больше процентов.

Короткие решения не сравниваем: у задачи в пять строк хорошие решения
совпадают сами по себе.
"""

from __future__ import annotations

import builtins
import keyword
import re
from dataclasses import dataclass, field
from itertools import combinations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Assignment, Problem, ProblemSetItem, User, utcnow
from app.services import solutions
from app.services.progress import participants_for_assignment, problems_for_set

K = 5
MIN_TOKENS = 30
# Ниже — обычное сходство решений одной задачи, показывать незачем: при 60 %
# отчёт шумел, у простых задач столько совпадает и у честных решений.
THRESHOLD = 0.7
# Между группами порог выше: решений в сравнении больше — и случайных
# совпадений тоже, а воронку расширяют, чтобы поймать явное.
ACROSS_THRESHOLD = 0.8
NEARLY_SAME = 0.9
# Редкие имена ищем, только когда есть с чем сравнить: у двоих решений
# любое общее имя «встречается только у них».
MIN_FOR_NAMES = 3
MAX_NAMES = 8

# Что остаётся в скелете как есть. Встроенные Python берём у самого Python;
# для C++ и Java — то, что пишут в олимпиадных решениях.
KEEP = frozenset(
    keyword.kwlist
    + [name for name in dir(builtins) if name.islower() and not name.startswith("_")]
    + """
    append pop split strip join sort extend insert remove get items keys values add update
    readline stdin stdout write read count index find replace lower upper setdefault
    sys math collections heapq bisect itertools functools deque defaultdict Counter
    heappush heappop heapify bisect_left bisect_right gcd sqrt inf lru_cache cache
    permutations combinations accumulate product
    int long double char bool void float short unsigned signed auto const static struct
    class public private protected return if else for while do break continue switch case
    default using namespace std include define typedef template typename true false
    nullptr new delete main cin cout cerr endl vector map set unordered_map unordered_set
    pair string queue stack priority_queue size push_back pop_back emplace_back begin end
    rbegin rend sort reverse swap first second make_pair ios sync_with_stdio tie memset
    sizeof lower_bound upper_bound erase clear empty front back top push greater less
    scanf printf ll final System out println Scanner nextInt nextLine String ArrayList
    HashMap List Map length
    """.split()
)

# Имена, которые пишет каждый второй или которые даёт условие (сигнатура
# LeetCode: nums, target). Даже встретившись только у двоих, уликой они
# не бывают — в отчёт «редких имён» не попадают.
COMMON_NAMES = frozenset(
    """
    nums num arr array target ans answer res result results total count cnt cur curr
    prev nxt left right mid low high lo hi start end begin dp memo cache seen visited
    graph grid matrix edges edge adj nodes node root head tail queue stack heap pairs
    pair best temp tmp val value values key keys idx index pos item items line lines
    data text word words string length size sum_ max_ min_ self solve solution
    Solution main input_data
    """.split()
)

TOKEN = re.compile(
    r"""
      (?P<string>"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*')
    | (?P<number>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)
    | (?P<name>[A-Za-z_]\w*)
    | (?P<op>[^\s\w])
    """,
    re.X,
)
# Комментарии: у Python «//» — деление, у остальных — комментарий; «#» у
# C++ — строка препроцессора, она одинакова у всех и тоже не нужна.
PY_COMMENTS = re.compile(r"#[^\n]*")
C_COMMENTS = re.compile(r"//[^\n]*|/\*.*?\*/|^\s*#[^\n]*", re.S | re.M)


def is_python(language: str | None) -> bool:
    """Присланный к задаче код — питоновский (курс на Python), у посылки
    язык записан: «Python 3», «PyPy 3-64», «GNU G++17»."""
    return language is None or "py" in language.lower()


@dataclass(slots=True)
class Fingerprint:
    tokens: int
    pieces: frozenset[int]
    names: frozenset[str]


def fingerprint(code: str, language: str | None = None) -> Fingerprint:
    text = (PY_COMMENTS if is_python(language) else C_COMMENTS).sub(" ", code)
    skeleton: list[str] = []
    names: set[str] = set()
    for match in TOKEN.finditer(text):
        kind, value = match.lastgroup, match.group()
        if kind == "string":
            skeleton.append("S")
        elif kind == "number":
            skeleton.append("N")
        elif kind == "name":
            if value in KEEP:
                skeleton.append(value)
            else:
                skeleton.append("V")
                # Однобуквенные i, j, n и обычные имена есть у всех — уликой не бывают.
                if len(value) >= 3 and value not in COMMON_NAMES:
                    names.add(value)
        else:
            skeleton.append(value)
    pieces = frozenset(hash(tuple(skeleton[i:i + K])) for i in range(len(skeleton) - K + 1))
    return Fingerprint(tokens=len(skeleton), pieces=pieces, names=frozenset(names))


def similarity(a: Fingerprint, b: Fingerprint) -> float:
    if not a.pieces or not b.pieces:
        return 0.0
    return len(a.pieces & b.pieces) / len(a.pieces | b.pieces)


@dataclass(slots=True)
class Pair:
    first: int
    second: int
    score: float
    shared_names: list[str]

    @property
    def percent(self) -> int:
        return round(self.score * 100)

    @property
    def nearly_same(self) -> bool:
        return self.score >= NEARLY_SAME


@dataclass(slots=True)
class Comparison:
    pairs: list[Pair]
    compared: int
    short: int


def compare(prints: dict[int, Fingerprint], focus: set[int] | None = None) -> Comparison:
    """Пары решений не ниже порога — сначала самые похожие.

    `focus` — только пары, где хотя бы один из этих людей: сравниваем группу
    с другими, а пары целиком внутри другой группы — на её странице. Редкие
    имена при этом считаются по всем решениям: чем их больше, тем весомее
    «есть только у этих двоих».
    """
    long_enough = {key: fp for key, fp in prints.items() if fp.tokens >= MIN_TOKENS}
    seen_in: dict[str, int] = {}
    for fp in long_enough.values():
        for name in fp.names:
            seen_in[name] = seen_in.get(name, 0) + 1

    pairs = []
    for (a, fa), (b, fb) in combinations(sorted(long_enough.items()), 2):
        if focus is not None and a not in focus and b not in focus:
            continue
        score = similarity(fa, fb)
        if score < THRESHOLD:
            continue
        shared: list[str] = []
        if len(long_enough) >= MIN_FOR_NAMES:
            shared = sorted(n for n in fa.names & fb.names if seen_in[n] == 2)[:MAX_NAMES]
        pairs.append(Pair(a, b, score, shared))
    pairs.sort(key=lambda p: (-p.score, -len(p.shared_names)))
    compared = len(long_enough) if focus is None else len(focus & set(long_enough))
    short = (len(prints) if focus is None else len(focus & set(prints))) - compared
    return Comparison(pairs=pairs, compared=compared, short=short)


# ---------------------------------------------------------------- по заданию


@dataclass(slots=True)
class Match:
    first: User
    second: User
    first_solution: solutions.Picked
    second_solution: solutions.Picked
    pair: Pair
    # Из какого задания каждое решение: у пары из разных групп — разные.
    first_from: Assignment
    second_from: Assignment

    @property
    def across(self) -> bool:
        return self.first_from.id != self.second_from.id


@dataclass(slots=True)
class ProblemReport:
    problem: Problem
    compared: int
    short: int
    matches: list[Match] = field(default_factory=list)
    # Другие задания с этой задачей, чьи решения вошли в сравнение.
    others: list[Assignment] = field(default_factory=list)


async def _students(session: AsyncSession, assignment: Assignment) -> list[User]:
    # Решения преподавателя не сравниваем — он прорешивает задачи заранее.
    return [u for u in await participants_for_assignment(session, assignment) if not u.is_teacher]


async def _other_assignments(
    session: AsyncSession, assignment: Assignment, problem_ids: list[int]
) -> list[Assignment]:
    """Задания с теми же задачами — других групп, по тому же списку или по
    другому, где задача тоже стоит. Только начавшиеся: в будущих решать
    ещё нечего."""
    rows = await session.execute(
        select(Assignment)
        .join(ProblemSetItem, ProblemSetItem.problem_set_id == Assignment.problem_set_id)
        .where(
            ProblemSetItem.problem_id.in_(problem_ids),
            Assignment.id != assignment.id,
            Assignment.assigned_at <= utcnow(),
        )
        .distinct()
        .order_by(Assignment.assigned_at)
    )
    return list(rows.scalars())


async def for_assignment(
    session: AsyncSession,
    assignment: Assignment,
    problem_id: int | None = None,
    *,
    across_groups: bool = False,
) -> list[ProblemReport]:
    """Похожие решения по каждой задаче задания — для страницы и для MCP.

    Решения те же, что видит проверяющий (`solutions.picked_for_assignment`).
    С `across_groups` к решениям группы добавляются решения той же задачи из
    других заданий: контрольную часто выдают нескольким группам сразу, и
    списывают через группу. Показываем пары, где хотя бы один — из этой
    группы; пара из разных групп — от `ACROSS_THRESHOLD`. У кого задача была
    в двух заданиях, берём решение отсюда: сам с собой человек не
    сравнивается.
    """
    here = await _students(session, assignment)
    problems = await problems_for_set(session, assignment.problem_set_id)
    if problem_id is not None:
        problems = [p for p in problems if p.id == problem_id]
    problem_ids = [p.id for p in problems]

    # (assignment, students, picked) — сначала своё задание: его решения главнее.
    sources = [(assignment, here, await solutions.picked_for_assignment(
        session, assignment, [u.id for u in here], problem_ids
    ))]
    if across_groups and problem_ids:
        for other in await _other_assignments(session, assignment, problem_ids):
            students = await _students(session, other)
            if not students:
                continue
            sources.append((other, students, await solutions.picked_for_assignment(
                session, other, [u.id for u in students], problem_ids
            )))

    users = {u.id: u for _, students, _ in sources for u in students}
    focus = {u.id for u in here}
    reports = []
    for problem in problems:
        pool: dict[int, tuple[solutions.Picked, Assignment]] = {}
        involved: list[Assignment] = []
        for source, _, picked in sources:
            for (user_id, pid), found in picked.items():
                if pid != problem.id or not found.code or user_id in pool:
                    continue
                pool[user_id] = (found, source)
                if source.id != assignment.id and source not in involved:
                    involved.append(source)
        result = compare(
            {uid: fingerprint(found.code, found.language) for uid, (found, _) in pool.items()},
            focus=focus,
        )
        reports.append(ProblemReport(
            problem=problem,
            compared=result.compared,
            short=result.short,
            others=involved,
            matches=[
                match
                for match in (
                    Match(
                        users[p.first], users[p.second],
                        pool[p.first][0], pool[p.second][0], p,
                        pool[p.first][1], pool[p.second][1],
                    )
                    for p in result.pairs
                )
                if not match.across or match.pair.score >= ACROSS_THRESHOLD
            ],
        ))
    return reports
