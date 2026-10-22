"""Праздничные темы: когда какая включается.

Праздник — это тема плюс украшения смайликами, и живёт он только в свои
даты. Тем, кто тему не выбирал, в праздник праздничная тема ставится сама;
у кого выбрана своя — у аватарки появляется значок праздника, и одно
нажатие включает тему. Праздник прошёл — сохранённый праздничный выбор
больше не действует, и человек возвращается к теме по умолчанию: тыквы
в декабре — уже не праздник, а забытая гирлянда.

Даты — по поясу портала (`DISPLAY_TIMEZONE`), включительно с обоих концов.
Сервер решает, идёт ли праздник, и сообщает это странице атрибутом
`data-season` у <html>; всё остальное делает браузер.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.i18n import mark as N_


@dataclass(frozen=True, slots=True)
class Season:
    # Ключ праздника — он же ключ темы в style.css и theme.js.
    key: str
    title: str
    emoji: str
    start: tuple[int, int]   # (месяц, день)
    end: tuple[int, int]

    def includes(self, today: date) -> bool:
        point = (today.month, today.day)
        if self.start <= self.end:
            return self.start <= point <= self.end
        # Праздник через Новый год: декабрь … январь.
        return point >= self.start or point <= self.end


SEASONS: tuple[Season, ...] = (
    Season("halloween", N_("Хэллоуин"), "🎃", (10, 22), (11, 1)),
    Season("newyear", N_("Новый год"), "🎄", (12, 1), (1, 10)),
    # Неделя до праздника и день после: 14-е выпадает и на выходные.
    Season("valentine", N_("День святого Валентина"), "💘", (2, 7), (2, 15)),
    # 12 апреля, неделя до и неделя после.
    Season("cosmos", N_("День космонавтики"), "🚀", (4, 5), (4, 19)),
)

SEASON_KEYS: tuple[str, ...] = tuple(season.key for season in SEASONS)


def current(today: date) -> Season | None:
    for season in SEASONS:
        if season.includes(today):
            return season
    return None
