"""Цветовые темы: список один на CSS, шаблон и скрипт, и набор переменных общий.

Тема, у которой не хватает переменной, ломается не сразу и не везде: страница
открывается, а цвет берётся от предыдущей темы или из умолчания браузера.
Поэтому проверяем не картинку, а полноту наборов.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.templating import THEME_KEYS

ROOT = Path(__file__).resolve().parent.parent
CSS = (ROOT / "app/static/style.css").read_text()
SCRIPT = (ROOT / "app/static/theme.js").read_text()
HEAD = (ROOT / "app/templates/base.html").read_text()

BLOCK = re.compile(r':root(?:\[data-theme="(?P<name>[a-z]+)"\])?\s*\{(?P<body>[^}]*)\}')


def blocks() -> dict[str, set[str]]:
    """Переменные каждой темы. Первый безымянный блок — тема по умолчанию."""
    found: dict[str, set[str]] = {}
    for match in BLOCK.finditer(CSS):
        names = re.findall(r"--([a-z0-9-]+):", match.group("body"))
        if not names:
            continue
        found.setdefault(match.group("name") or "amber", set()).update(names)
    return found


def test_every_theme_defines_the_same_variables():
    sets = blocks()
    first = sets["amber"]
    for key in THEME_KEYS:
        assert key in sets, f"темы {key} нет в style.css"
        missing = first - sets[key] - {"rule", "display", "sans", "mono", "rail"}
        assert not missing, f"в теме {key} не хватает: {sorted(missing)}"


def test_the_system_theme_is_gone_but_its_choice_is_not_lost():
    """Тему убрали; у кого она была — получает по светлоте системы, а не сброс."""
    assert '[data-theme="auto"]' not in CSS
    assert "auto" not in THEME_KEYS
    for source in (SCRIPT, HEAD):
        assert 'prefers-color-scheme: dark' in source
        assert '"coal" : "amber"' in source


def test_the_list_of_themes_matches_everywhere():
    """Список тем повторён в трёх местах — разъехавшись, он ломает выбор молча."""
    keys = list(THEME_KEYS)
    in_script = re.search(r"var THEMES = \[(.*?)\]", SCRIPT, re.S).group(1)
    assert sorted(re.findall(r'"([a-z]+)"', in_script)) == sorted(keys)

    in_head = re.search(r'indexOf\(t\)', HEAD)
    assert in_head, "инлайновый скрипт в <head> больше не проверяет тему"
    listed = re.search(r'\[((?:"[a-z]+",?\s*)+)\]\.indexOf\(t\)', HEAD).group(1)
    assert sorted(re.findall(r'"([a-z]+)"', listed)) == sorted(keys)


def test_old_light_and_dark_choices_still_work():
    """У людей в браузере уже сохранено light/dark — выбор терять нельзя."""
    for source in (SCRIPT, HEAD):
        assert 'light: "amber"' in source and 'dark: "coal"' in source
        # И первые имена самих тем: их успели выбрать, пока набор менялся.
        assert 'poster: "amber"' in source and 'indigo: "oxford"' in source


def test_old_choices_lead_to_themes_that_exist():
    """Набор менялся дважды; запись, ведущая на убранную тему, молча теряет выбор."""
    themes = set(THEME_KEYS)
    listed = re.search(r"var LEGACY = \{(.*?)\};", SCRIPT, re.S).group(1)
    for old, new in re.findall(r'(\w+): "([a-z]+)"', listed):
        assert new in themes, f"«{old}» ведёт на несуществующую тему «{new}»"
