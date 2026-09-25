"""Редактор кода на странице задачи.

Проверяем не поведение браузера, а то, на чём портал уже обжёгся: стили
CodeMirror подключаются после наших и при равной силе побеждают. Редактор
тогда остаётся белым на любой теме, а код набран палитрой для белого листа —
на «Угле» его почти не видно.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSS = (ROOT / "app/static/style.css").read_text()
SCRIPT = (ROOT / "app/static/editor.js").read_text()
PAGE = (ROOT / "app/templates/task.html").read_text()

# Цвета этих токенов CodeMirror задаёт сам — через имя своей темы.
OVERRIDDEN = ("cm-keyword", "cm-string", "cm-comment", "cm-number", "cm-def")


def test_our_colors_outweigh_the_ones_codemirror_brings():
    for token in OVERRIDDEN:
        rules = re.findall(r"^([^\n{]*\." + token + r")\s*[,{]", CSS, re.M)
        assert rules, f"цвет {token} не переопределён"
        for selector in rules:
            assert ".cm-s-default" in selector, (
                f"{selector.strip()} слабее, чем `.cm-s-default .{token}` у CodeMirror"
            )


def test_editor_takes_colors_from_the_theme():
    """Фон и текст — переменные темы, иначе редактор живёт своей жизнью."""
    rule = re.search(r"\.CodeMirror\.CodeMirror \{(.*?)\}", CSS, re.S).group(1)
    assert "var(--panel)" in rule and "var(--text)" in rule
    # Удвоенный селектор — то самое, чем мы перебиваем чужие стили.
    assert ".CodeMirror-gutters.CodeMirror-gutters" in CSS


def test_editor_grows_with_the_code():
    """Фиксированная высота оставляла пустое поле под коротким решением."""
    rule = re.search(r"\.CodeMirror\.CodeMirror \{(.*?)\}", CSS, re.S).group(1)
    assert "height: auto" in rule
    assert "min-height" in rule and "max-height" in rule


def test_the_suggestion_list_is_dressed_by_the_portal():
    """Тот же капкан, что и с подсветкой: аддон красит список после нас."""
    for selector in (".CodeMirror-hints.CodeMirror-hints",
                     ".CodeMirror-hints li.CodeMirror-hint",
                     ".CodeMirror-hints li.CodeMirror-hint-active"):
        assert selector in CSS, f"{selector} слабее, чем правило аддона"
    assert "var(--panel)" in CSS.split(".CodeMirror-hints.CodeMirror-hints")[1][:200]


def test_suggestions_know_python_and_stay_out_of_strings():
    assert '"Ctrl-Space"' in SCRIPT
    for word in ("enumerate", "return", "lambda"):
        assert word in SCRIPT, f"{word} не предлагается"
    # В строке и в комментарии подсказывать нечего.
    assert 'token === "string"' in SCRIPT and 'token === "comment"' in SCRIPT


def test_shortcuts_are_bound_and_shown():
    """Сочетание, о котором никто не знает, не экономит ни одного нажатия."""
    for key in ("Ctrl-Enter", "Ctrl-/", "Alt-Up", "Alt-Down", "Ctrl-S", "Ctrl-Space"):
        assert f'"{key}"' in SCRIPT, f"{key} не привязан"
    for hint in ("Ctrl", "Enter", "Alt", "Tab", "Space"):
        assert hint in PAGE, f"о {hint} на странице не сказано"
