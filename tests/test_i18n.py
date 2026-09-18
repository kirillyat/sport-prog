"""Язык интерфейса: выбор, запасной вариант и то, что перевод доезжает до страницы."""

from __future__ import annotations

import json
import re

import httpx

from app.config import settings
from app.i18n import (
    CATALOGS,
    LANGUAGES,
    LOCALES_DIR,
    SOURCE_LANGUAGE,
    pick_language,
)


async def _login(client: httpx.AsyncClient, name: str = "Аня"):
    return await client.post("/login/dev", data={"name": name})


def test_cookie_wins_over_browser():
    assert pick_language("fr", "en-US,en;q=0.9") == "fr"


def test_browser_hint_used_on_first_visit():
    assert pick_language(None, "en-GB,en;q=0.9") == "en"
    assert pick_language(None, "de-DE,de;q=0.9,fr;q=0.5") == "fr"


def test_unknown_language_falls_back_to_russian():
    assert pick_language("kz", "de-DE") == SOURCE_LANGUAGE
    assert pick_language(None, None) == SOURCE_LANGUAGE


def test_portal_default_closes_the_chain(monkeypatch):
    """Вуз может поставить свой язык по умолчанию — но выбор человека главнее."""
    monkeypatch.setattr(settings, "default_language", "fr")
    assert pick_language(None, "de-DE") == "fr"
    assert pick_language("en", "de-DE") == "en"
    assert pick_language(None, "ru-RU,ru") == "ru"


def test_catalogs_cover_the_same_strings():
    """Пустое значение в словаре — тоже пропуск: страница останется русской."""
    for code in LANGUAGES:
        if code == SOURCE_LANGUAGE:
            continue
        catalog = json.loads((LOCALES_DIR / f"{code}.json").read_text(encoding="utf-8"))
        missing = [key for key, value in catalog.items() if not value]
        assert not missing, f"{code}: без перевода {missing[:5]}"
    assert set(CATALOGS) == set(LANGUAGES) - {SOURCE_LANGUAGE}


def test_placeholders_survive_translation():
    """`%(count)s` в переводе обязан остаться: иначе подстановка упадёт при выводе."""
    holder = re.compile(r"%\([a-z_]+\)s")
    for code in LANGUAGES:
        if code == SOURCE_LANGUAGE:
            continue
        catalog = json.loads((LOCALES_DIR / f"{code}.json").read_text(encoding="utf-8"))
        for source, translated in catalog.items():
            assert set(holder.findall(source)) == set(holder.findall(translated)), (
                f"{code}: подстановки разъехались в {source!r}"
            )


def test_plural_forms_match_the_language():
    """Формы через «|»: три у русского, две у английского и французского."""
    for code in LANGUAGES:
        if code == SOURCE_LANGUAGE:
            continue
        catalog = json.loads((LOCALES_DIR / f"{code}.json").read_text(encoding="utf-8"))
        for source, translated in catalog.items():
            if "|" not in source:
                assert "|" not in translated, f"{code}: лишние формы в {source!r}"
                continue
            assert source.count("|") == 2, f"русских форм должно быть три: {source!r}"
            assert translated.count("|") == 1, f"{code}: нужны две формы в {source!r}"


async def test_login_page_speaks_the_browser_language(session, client):
    response = await client.get("/login", headers={"accept-language": "en-US,en;q=0.9"})
    assert 'lang="en"' in response.text
    assert "Local sign-in" in response.text
    assert "Тренировки, задания и рейтинг" not in response.text


async def test_switcher_sets_cookie_and_translates_rail(session, client):
    await _login(client)
    response = await client.post("/view/lang", data={"lang": "fr", "next": "/"})
    assert response.status_code == 200
    assert client.cookies.get("lang") == "fr"
    assert 'lang="fr"' in response.text
    assert "Annonces" in response.text
    assert ">Анонсы<" not in response.text


async def test_switcher_ignores_unknown_language(session, client):
    await _login(client)
    await client.post("/view/lang", data={"lang": "xx", "next": "/"})
    assert client.cookies.get("lang") is None
    assert ">Анонсы<" in (await client.get("/")).text


async def test_switcher_does_not_leave_the_portal(session, client):
    await _login(client)
    response = await client.post(
        "/view/lang", data={"lang": "en", "next": "//evil.example"}, follow_redirects=False
    )
    assert response.headers["location"] == "/"


async def test_scripts_get_translated_strings(session, client):
    await _login(client)
    await client.post("/view/lang", data={"lang": "en"})
    response = await client.get("/")
    assert '"theme.dark": "Theme: dark"' in response.text
    assert '"countdown.days": "day|days"' in response.text
