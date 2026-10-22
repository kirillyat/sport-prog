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

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Assignment, Problem, User
from app.services import solutions
from app.services.progress import participants_for_assignment, problems_for_set

K = 5
MIN_TOKENS = 30
# Ниже — обычное сходство решений одной задачи, показывать незачем.
THRESHOLD = 0.6
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


def compare(prints: dict[int, Fingerprint]) -> Comparison:
    """Пары решений не ниже порога — сначала самые похожие."""
    long_enough = {key: fp for key, fp in prints.items() if fp.tokens >= MIN_TOKENS}
    seen_in: dict[str, int] = {}
    for fp in long_enough.values():
        for name in fp.names:
            seen_in[name] = seen_in.get(name, 0) + 1

    pairs = []
    for (a, fa), (b, fb) in combinations(sorted(long_enough.items()), 2):
        score = similarity(fa, fb)
        if score < THRESHOLD:
            continue
        shared: list[str] = []
        if len(long_enough) >= MIN_FOR_NAMES:
            shared = sorted(n for n in fa.names & fb.names if seen_in[n] == 2)[:MAX_NAMES]
        pairs.append(Pair(a, b, score, shared))
    pairs.sort(key=lambda p: (-p.score, -len(p.shared_names)))
    return Comparison(pairs=pairs, compared=len(long_enough), short=len(prints) - len(long_enough))


# ---------------------------------------------------------------- по заданию


@dataclass(slots=True)
class Match:
    first: User
    second: User
    first_solution: solutions.Picked
    second_solution: solutions.Picked
    pair: Pair


@dataclass(slots=True)
class ProblemReport:
    problem: Problem
    compared: int
    short: int
    matches: list[Match] = field(default_factory=list)


async def for_assignment(
    session: AsyncSession, assignment: Assignment, problem_id: int | None = None
) -> list[ProblemReport]:
    """Похожие решения по каждой задаче задания — для страницы и для MCP.

    Решения те же, что видит проверяющий (`solutions.picked_for_assignment`);
    решения преподавателя не сравниваем — он прорешивает задачи заранее.
    """
    students = [
        u for u in await participants_for_assignment(session, assignment) if not u.is_teacher
    ]
    by_id = {u.id: u for u in students}
    problems = await problems_for_set(session, assignment.problem_set_id)
    if problem_id is not None:
        problems = [p for p in problems if p.id == problem_id]
    picked = await solutions.picked_for_assignment(
        session, assignment, list(by_id), [p.id for p in problems]
    )

    reports = []
    for problem in problems:
        mine = {
            user_id: found
            for (user_id, pid), found in picked.items()
            if pid == problem.id and found.code
        }
        result = compare({
            user_id: fingerprint(found.code, found.language) for user_id, found in mine.items()
        })
        reports.append(ProblemReport(
            problem=problem,
            compared=result.compared,
            short=result.short,
            matches=[
                Match(by_id[p.first], by_id[p.second], mine[p.first], mine[p.second], p)
                for p in result.pairs
            ],
        ))
    return reports
