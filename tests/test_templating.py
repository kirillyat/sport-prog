from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.templating import (
    LOCAL_TZ,
    avatar_hue,
    day_label,
    group_by_day,
    initials,
    plural_ru,
    timeago,
)


@pytest.mark.parametrize(
    ("count", "expected"),
    [(1, "минуту"), (2, "минуты"), (4, "минуты"), (5, "минут"),
     (11, "минут"), (14, "минут"), (21, "минуту"), (22, "минуты"), (25, "минут"), (0, "минут")],
)
def test_russian_plurals(count, expected):
    assert plural_ru(count, "минуту", "минуты", "минут") == expected


def test_timeago_scales():
    # Полдень по местному времени: иначе «5 часов назад» у границы суток
    # превращается во «вчера», и тест зависит от часа запуска.
    now = datetime.now(LOCAL_TZ).replace(hour=12, minute=0).astimezone(UTC)
    assert timeago(now - timedelta(seconds=10), now) == "только что"
    assert timeago(now - timedelta(minutes=1), now) == "1 минуту назад"
    assert timeago(now - timedelta(minutes=5), now) == "5 минут назад"
    assert timeago(now - timedelta(hours=2), now) == "2 часа назад"
    assert timeago(now - timedelta(hours=5), now) == "5 часов назад"
    assert timeago(None) == "—"


def test_timeago_yesterday_uses_local_day():
    # Вечер минус сутки с небольшим: и «вчера» по календарю, и больше 24 часов.
    now = datetime.now(LOCAL_TZ).replace(hour=20, minute=0)
    yesterday = now.replace(hour=12, minute=30) - timedelta(days=1)
    assert timeago(yesterday.astimezone(UTC), now.astimezone(UTC)) == "вчера в 12:30"


@pytest.mark.parametrize(
    ("name", "expected"),
    [("Аня", "А"), ("Пётр Иванов", "ПИ"), ("Anna Maria Smith", "AM"), ("", "?"), (None, "?")],
)
def test_initials(name, expected):
    assert initials(name) == expected


def test_avatar_hue_is_stable_and_in_range():
    assert avatar_hue("Аня") == avatar_hue("Аня")
    assert avatar_hue("Аня") != avatar_hue("Боря")
    assert 0 <= avatar_hue("кто угодно") < 360


def test_day_label():
    now = datetime.now(LOCAL_TZ).replace(hour=12)
    assert day_label(now) == "Сегодня"
    assert day_label(now - timedelta(days=1)) == "Вчера"
    old = datetime(2020, 9, 12, 12, tzinfo=LOCAL_TZ)
    assert day_label(old) == "12 сентября 2020"


class _Item:
    def __init__(self, solved_at):
        self.solved_at = solved_at


def test_group_by_day_preserves_order():
    now = datetime.now(LOCAL_TZ).replace(hour=12)
    items = [
        _Item(now),
        _Item(now - timedelta(minutes=30)),
        _Item(now - timedelta(days=1)),
        _Item(now - timedelta(days=1, minutes=10)),
    ]
    grouped = group_by_day(items)
    assert [label for label, _ in grouped] == ["Сегодня", "Вчера"]
    assert [len(chunk) for _, chunk in grouped] == [2, 2]


def test_difficulty_sorting():
    from app.services.stats import _difficulty_order

    keys = ["2000+", "Hard", "800+", "Easy", "1400+", "Medium"]
    assert sorted(keys, key=lambda k: _difficulty_order((k, 0))) == [
        "Easy", "Medium", "Hard", "800+", "1400+", "2000+",
    ]


def test_sentence_keeps_abbreviations():
    """«учётная запись МГУ» → «Учётная запись МГУ», а не «Учётная запись мгу»."""
    from app.templating import sentence

    assert sentence("учётная запись МГУ") == "Учётная запись МГУ"
    assert sentence("") == ""
    assert sentence(None) == ""
