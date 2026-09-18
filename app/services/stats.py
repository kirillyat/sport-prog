from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.i18n import mark as N_
from app.models import Platform, Problem, Submission, User

HEATMAP_WEEKS = 18

# Переводятся при выводе: календарь собирается вне запроса.
MONTHS_SHORT = (
    N_("янв"), N_("фев"), N_("мар"), N_("апр"), N_("май"), N_("июн"),
    N_("июл"), N_("авг"), N_("сен"), N_("окт"), N_("ноя"), N_("дек"),
)

LEETCODE_ORDER = {"easy": 0, "medium": 1, "hard": 2}


def _difficulty_order(item: tuple[str, int]) -> tuple[int, float, str]:
    """Easy → Medium → Hard, затем рейтинговые корзины Codeforces по возрастанию."""
    key = item[0]
    lowered = key.lower()
    if lowered in LEETCODE_ORDER:
        return (0, LEETCODE_ORDER[lowered], key)
    digits = key.rstrip("+")
    if digits.isdigit():
        return (1, int(digits), key)
    return (2, 0, key)


@dataclass(slots=True)
class HeatmapDay:
    day: date
    count: int
    future: bool = False

    @property
    def level(self) -> int:
        if self.future or self.count == 0:
            return 0
        if self.count >= 8:
            return 4
        if self.count >= 4:
            return 3
        if self.count >= 2:
            return 2
        return 1


@dataclass(slots=True)
class Heatmap:
    weeks: list[list[HeatmapDay]] = field(default_factory=list)
    # индекс недели -> подпись месяца над ней
    month_labels: dict[int, str] = field(default_factory=dict)
    active_days: int = 0
    total: int = 0

    @property
    def days_covered(self) -> int:
        return len(self.weeks) * 7


@dataclass(slots=True)
class UserStats:
    total_accepted: int = 0
    unique_problems: int = 0
    by_difficulty: dict[str, int] = field(default_factory=dict)
    by_platform: dict[str, int] = field(default_factory=dict)
    top_tags: list[tuple[str, int]] = field(default_factory=list)
    heatmap: Heatmap = field(default_factory=Heatmap)
    current_streak: int = 0
    longest_streak: int = 0
    first_solve: date | None = None
    last_solve: date | None = None

    @property
    def max_difficulty(self) -> int:
        return max(self.by_difficulty.values(), default=0)

    @property
    def max_platform(self) -> int:
        return max(self.by_platform.values(), default=0)

    @property
    def max_tag(self) -> int:
        return max((count for _, count in self.top_tags), default=0)


def _streaks(days: set[date]) -> tuple[int, int]:
    if not days:
        return 0, 0
    ordered = sorted(days)
    longest = current = 1
    for prev, cur in zip(ordered, ordered[1:], strict=False):
        current = current + 1 if (cur - prev).days == 1 else 1
        longest = max(longest, current)

    today = date.today()
    streak = 0
    cursor = today if today in days else today - timedelta(days=1)
    while cursor in days:
        streak += 1
        cursor -= timedelta(days=1)
    return streak, longest


def build_heatmap(per_day: dict[date, int], weeks: int = HEATMAP_WEEKS) -> Heatmap:
    """Календарь как на GitHub: строка — день недели, столбец — неделя.

    Выравнивание по понедельнику обязательно: без него дни уезжают по строкам
    и картинка перестаёт читаться как календарь.
    """
    today = date.today()
    last_sunday = today + timedelta(days=6 - today.weekday())
    start = last_sunday - timedelta(days=weeks * 7 - 1)

    heatmap = Heatmap()
    previous_month = None
    for week in range(weeks):
        column: list[HeatmapDay] = []
        for offset in range(7):
            day = start + timedelta(days=week * 7 + offset)
            count = per_day.get(day, 0)
            column.append(HeatmapDay(day=day, count=count, future=day > today))
        heatmap.weeks.append(column)

        first_day = start + timedelta(days=week * 7)
        if first_day.month != previous_month:
            # Подпись ставим только если у месяца есть запас столбцов справа.
            if week == 0 or first_day.day <= 7:
                heatmap.month_labels[week] = MONTHS_SHORT[first_day.month - 1]
            previous_month = first_day.month

    visible = [d for column in heatmap.weeks for d in column if not d.future]
    heatmap.active_days = sum(1 for d in visible if d.count)
    heatmap.total = sum(d.count for d in visible)
    return heatmap


async def user_stats(session: AsyncSession, user: User) -> UserStats:
    stats = UserStats()

    rows = (
        await session.execute(
            select(Submission, Problem)
            .outerjoin(Problem, Problem.id == Submission.problem_id)
            .where(Submission.user_id == user.id, Submission.is_accepted.is_(True))
        )
    ).all()

    stats.total_accepted = len(rows)
    seen_problems: set[tuple[str, str]] = set()
    difficulty: Counter[str] = Counter()
    platform: Counter[str] = Counter()
    tags: Counter[str] = Counter()
    days: set[date] = set()

    for submission, problem in rows:
        key = (submission.platform.value, submission.problem_slug or submission.external_id)
        if key not in seen_problems:
            seen_problems.add(key)
            platform[Platform(submission.platform).title] += 1
            if problem is not None:
                if problem.difficulty:
                    difficulty[problem.difficulty] += 1
                elif problem.rating:
                    difficulty[f"{problem.rating // 200 * 200}+"] += 1
                for tag in problem.tags or []:
                    tags[tag] += 1
        days.add(submission.submitted_at.astimezone(UTC).date())

    stats.unique_problems = len(seen_problems)
    stats.by_difficulty = dict(sorted(difficulty.items(), key=_difficulty_order))
    stats.by_platform = dict(platform.most_common())
    stats.top_tags = tags.most_common(10)
    stats.current_streak, stats.longest_streak = _streaks(days)
    stats.first_solve = min(days) if days else None
    stats.last_solve = max(days) if days else None

    per_day = {
        date.fromisoformat(raw): int(count)
        for raw, count in (
            await session.execute(
                select(func.date(Submission.submitted_at), func.count())
                .where(Submission.user_id == user.id, Submission.is_accepted.is_(True))
                .group_by(func.date(Submission.submitted_at))
            )
        ).all()
    }
    stats.heatmap = build_heatmap(per_day)
    return stats
