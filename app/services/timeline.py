"""Когда решали задание: посылки участников по времени, от выдачи до дедлайна.

Для преподавателя — увидеть, решают ли задание по ходу недели или всё
приходит в последний вечер. Поэтому в столбике две части: первое принятое
решение задачи (то, что идёт в зачёт) и остальные посылки — попытки,
ошибки, повторные сдачи. Отдельно считается доля решений, пришедших
в последние сутки перед дедлайном: ради этой цифры график и открывают.

Здесь же раскладка графика в координатах SVG. Рисует его шаблон, без
скриптов: столбики, подписи и подсказки при наведении — обычный SVG.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Assignment, Problem, Submission, User, utcnow

# Шаги, из которых выбираем ширину столбика: самый мелкий, при котором
# столбиков не больше MAX_BINS. Мельче часа смотреть незачем, а больше
# полусотни столбиков на экране ноутбука сливаются в гребёнку.
STEPS = (
    timedelta(hours=1),
    timedelta(hours=2),
    timedelta(hours=3),
    timedelta(hours=6),
    timedelta(hours=12),
    timedelta(days=1),
    timedelta(days=2),
    timedelta(days=7),
)
MAX_BINS = 48
LAST_DAY = timedelta(hours=24)


@dataclass(slots=True)
class Bin:
    start: datetime
    end: datetime
    solves: int = 0
    others: int = 0

    @property
    def total(self) -> int:
        return self.solves + self.others

    @property
    def last_day(self) -> datetime:
        """Последние сутки столбика — для подписи «12.10–13.10» у двухдневного шага."""
        return self.end - timedelta(days=1)


@dataclass(slots=True)
class Timeline:
    start: datetime
    end: datetime
    step: timedelta
    deadline: datetime | None
    bins: list[Bin] = field(default_factory=list)

    @property
    def solves(self) -> int:
        return sum(b.solves for b in self.bins)

    @property
    def submissions(self) -> int:
        return sum(b.total for b in self.bins)

    @property
    def peak(self) -> int:
        return max((b.total for b in self.bins), default=0)

    def last_day_share(self, solves_before: list[datetime]) -> int | None:
        """Сколько процентов решений в срок пришло в последние сутки перед дедлайном."""
        if self.deadline is None or not solves_before:
            return None
        late = sum(1 for moment in solves_before if moment > self.deadline - LAST_DAY)
        return round(late / len(solves_before) * 100)


def pick_step(span: timedelta) -> timedelta:
    for step in STEPS:
        if math.ceil(span / step) <= MAX_BINS:
            return step
    return STEPS[-1]


def _floor(moment: datetime, step: timedelta, tz) -> datetime:
    """Начало столбика по местным часам: «с 18:00», а не «с 17:43».

    Шаги до суток выравниваются от местной полуночи, сутки и больше — по ней же.
    """
    local = moment.astimezone(tz)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if step >= timedelta(days=1):
        return midnight
    hours = int(step.total_seconds() // 3600)
    return midnight + timedelta(hours=(local.hour // hours) * hours)


async def build(
    session: AsyncSession,
    assignment: Assignment,
    participants: list[User],
    problems: list[Problem],
    tz,
    now: datetime | None = None,
) -> tuple[Timeline, list[datetime]] | None:
    """Посылки участников по столбикам и моменты первых решений в срок.

    None — смотреть не на что: нет участников, задач или ни одной посылки.
    Окно — от выдачи до дедлайна; если после дедлайна ещё сдают — до
    последней посылки, но не дальше «сейчас».
    """
    now = now or utcnow()
    user_ids = [u.id for u in participants if not u.is_teacher]
    problem_ids = [p.id for p in problems]
    if not user_ids or not problem_ids:
        return None
    rows = (
        await session.execute(
            select(Submission.user_id, Submission.problem_id, Submission.submitted_at,
                   Submission.is_accepted)
            .where(
                Submission.user_id.in_(user_ids),
                Submission.problem_id.in_(problem_ids),
                Submission.submitted_at >= assignment.assigned_at,
                Submission.submitted_at <= now,
            )
            .order_by(Submission.submitted_at)
        )
    ).all()
    if not rows:
        return None

    # Окно — до дедлайна, даже если он впереди: пустой хвост показывает,
    # сколько времени у группы ещё есть. Сдают после дедлайна — до последней посылки.
    end = max(assignment.deadline or now, rows[-1].submitted_at)
    step = pick_step(end - assignment.assigned_at)
    start = _floor(assignment.assigned_at, step, tz)

    timeline = Timeline(start=start, end=end, step=step, deadline=assignment.deadline)
    cursor = start
    while cursor <= end:
        timeline.bins.append(Bin(start=cursor, end=cursor + step))
        cursor += step

    # Посылки идут по времени, поэтому первая принятая по паре «студент —
    # задача» — это и есть решение, которое засчитывается.
    first: dict[tuple[int, int], datetime] = {}
    for user_id, problem_id, moment, accepted in rows:
        index = min(int((moment - start) / step), len(timeline.bins) - 1)
        if accepted and (user_id, problem_id) not in first:
            first[(user_id, problem_id)] = moment
            timeline.bins[index].solves += 1
        else:
            timeline.bins[index].others += 1

    in_time = [
        moment for moment in first.values()
        if assignment.deadline is None or moment <= assignment.deadline
    ]
    return timeline, in_time


# ---------------------------------------------------------------- раскладка


@dataclass(slots=True)
class BarShape:
    x: float
    width: float
    solves_y: float
    solves_h: float
    others_y: float
    others_h: float
    bin: Bin


@dataclass(slots=True)
class Chart:
    width: int
    height: int
    left: int
    bottom: float
    bars: list[BarShape]
    y_ticks: list[tuple[float, int]]
    # (x, день, выравнивание): у краёв подпись прижата внутрь, иначе её режет рамка.
    x_labels: list[tuple[float, datetime, str]]
    deadline_x: float | None
    after_deadline_x: float | None


WIDTH, HEIGHT = 720, 220
LEFT, RIGHT, TOP, BOTTOM = 34, 8, 14, 30
# Столбик не толще 24 точек и не во всю ширину ячейки — между ними воздух.
MAX_BAR = 24
GAP = 2


def _nice_max(peak: int) -> int:
    """Верх шкалы — круглое число не меньше пика: 4 → 5, 13 → 15, 37 → 40."""
    if peak <= 5:
        return max(peak, 1)
    magnitude = 10 ** int(math.log10(peak))
    for factor in (1, 2, 2.5, 5, 10):
        top = int(factor * magnitude)
        if top >= peak:
            return top
    return peak


def layout(timeline: Timeline, tz) -> Chart:
    plot_w = WIDTH - LEFT - RIGHT
    plot_h = HEIGHT - TOP - BOTTOM
    bottom = TOP + plot_h
    count = len(timeline.bins)
    slot = plot_w / count
    bar_w = min(MAX_BAR, max(2.0, slot - GAP * 2))
    top_value = _nice_max(timeline.peak)
    scale = plot_h / top_value

    bars = []
    for index, item in enumerate(timeline.bins):
        x = LEFT + index * slot + (slot - bar_w) / 2
        solves_h = item.solves * scale
        others_h = item.others * scale
        # Между частями столбика — зазор в цвет фона, а не обводка.
        gap = GAP if item.solves and item.others else 0
        solves_y = bottom - solves_h
        others_y = solves_y - gap - others_h
        bars.append(BarShape(x, bar_w, solves_y, solves_h, others_y, others_h, item))

    ticks = sorted({0, top_value // 2, top_value}) if top_value >= 2 else [0, top_value]
    y_ticks = [(bottom - value * scale, value) for value in ticks]

    def x_of(moment: datetime) -> float:
        return LEFT + (moment - timeline.start) / timeline.step * slot

    # Подписи по оси времени — на границах суток, не чаще чем помещается.
    span_end = timeline.start + timeline.step * count
    labels: list[tuple[float, datetime, str]] = []
    edge = 18  # полширины подписи «22.10» мелким шрифтом

    def anchor(x: float) -> str:
        if x > LEFT + plot_w - edge:
            return "end"
        if x < LEFT + edge:
            return "start"
        return "middle"

    day = timeline.start.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    if day < timeline.start:
        day += timedelta(days=1)
    days = max(1, (span_end - day).days + 1)
    every = max(1, math.ceil(days / 8))
    while day <= span_end:
        x = min(x_of(day), LEFT + plot_w)
        labels.append((x, day, anchor(x)))
        day += timedelta(days=every)
    if not labels:
        labels.append((LEFT, timeline.start.astimezone(tz), "start"))

    deadline_x = None
    after_x = None
    if timeline.deadline is not None and timeline.start <= timeline.deadline <= span_end:
        deadline_x = x_of(timeline.deadline)
        after_x = deadline_x if deadline_x < LEFT + plot_w - 1 else None
    return Chart(WIDTH, HEIGHT, LEFT, bottom, bars, y_ticks, labels, deadline_x, after_x)
