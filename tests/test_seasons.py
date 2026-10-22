"""Праздничные темы: когда включаются и как их предлагают."""

from __future__ import annotations

from datetime import date

import pytest

from app.services import seasons


@pytest.mark.parametrize(("day", "key"), [
    (date(2026, 10, 21), None),
    (date(2026, 10, 22), "halloween"),
    (date(2026, 10, 31), "halloween"),
    (date(2026, 11, 1), "halloween"),
    (date(2026, 11, 2), None),
    (date(2026, 11, 30), None),
    (date(2026, 12, 1), "newyear"),
    (date(2026, 12, 31), "newyear"),
    (date(2027, 1, 10), "newyear"),
    (date(2027, 1, 11), None),
    (date(2027, 2, 6), None),
    (date(2027, 2, 7), "valentine"),
    (date(2027, 2, 14), "valentine"),
    (date(2027, 2, 15), "valentine"),
    (date(2027, 2, 16), None),
    (date(2027, 4, 4), None),
    (date(2027, 4, 5), "cosmos"),
    (date(2027, 4, 12), "cosmos"),
    (date(2027, 4, 19), "cosmos"),
    (date(2027, 4, 20), None),
])
def test_holidays_have_their_dates_inclusive(day, key):
    season = seasons.current(day)
    assert (season.key if season else None) == key


async def _login(client):
    await client.post("/login/dev", data={"name": "Аня"})


# Подменяем сам календарь, а не глобал шаблонов: Jinja кэширует
# импортированный модуль меню тем вместе с тем, что видела при импорте.
@pytest.fixture
def halloween(monkeypatch):
    real = seasons.current
    monkeypatch.setattr(seasons, "current", lambda today: real(date(2026, 10, 31)))


@pytest.fixture
def no_holiday(monkeypatch):
    real = seasons.current
    monkeypatch.setattr(seasons, "current", lambda today: real(date(2026, 11, 15)))


async def test_in_a_holiday_the_page_offers_its_theme(session, client, halloween):
    await _login(client)
    page = (await client.get("/")).text
    assert '<html lang="ru" data-season="halloween">' in page
    # Значок у аватарки — и в рейке, и на телефоне; виден он только тем,
    # у кого своя тема, — это решает скрипт, поэтому в разметке он скрыт.
    assert page.count('data-season-btn hidden') == 2
    assert page.count('data-theme-set="halloween"') >= 3      # + пункт в меню тем
    assert "Хэллоуин" in page


async def test_outside_a_holiday_there_is_no_trace_of_it(session, client, no_holiday):
    await _login(client)
    page = (await client.get("/")).text
    assert "data-season" not in page.split("<head>")[0]
    assert "data-season-btn" not in page
    assert 'data-theme-set="halloween"' not in page


async def test_login_page_gets_the_holiday_too(session, client, halloween):
    """На странице входа рейки нет, но тема и гирлянда — общие для всего сайта."""
    page = (await client.get("/login")).text
    assert 'data-season="halloween"' in page
