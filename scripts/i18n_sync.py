#!/usr/bin/env python3
"""Собирает строки `_("…")` из шаблонов и кода в словари app/locales/*.json.

Запуск:  python scripts/i18n_sync.py

Новые строки попадают в словарь с пустым значением — их видно и понятно,
что осталось перевести. Уже переведённое не трогается, исчезнувшее из кода
убирается. Порядок ключей — как в исходниках, поэтому diff читаемый.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.i18n import DEFAULT_LANGUAGE, LANGUAGES, LOCALES_DIR  # noqa: E402

TEMPLATES = ROOT / "app" / "templates"
CODE = ROOT / "app"

# В шаблонах разбирать нечем, кроме регулярки: _("…") и N_("…").
CALL = re.compile(r"""(?<![\w])N?_\(\s*(?P<q>["'])(?P<text>(?:\\.|(?!(?P=q)).)*)(?P=q)\s*[),]""")


def unescape(raw: str, quote: str) -> str:
    return raw.replace("\\" + quote, quote).replace("\\\\", "\\")


def from_templates(seen: dict[str, None]) -> None:
    for path in sorted(TEMPLATES.rglob("*.html")):
        for match in CALL.finditer(path.read_text(encoding="utf-8")):
            seen.setdefault(unescape(match.group("text"), match.group("q")), None)


def from_code(seen: dict[str, None]) -> None:
    """Питон разбираем деревом: строки бывают склеены из нескольких кусков."""
    for path in sorted(CODE.rglob("*.py")):
        if path.name == "i18n.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else None
            if name not in {"_", "N_"}:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                seen.setdefault(first.value, None)


def collect() -> list[str]:
    seen: dict[str, None] = {}
    from_templates(seen)
    from_code(seen)
    return list(seen)


def main() -> int:
    keys = collect()
    LOCALES_DIR.mkdir(exist_ok=True)
    for code in LANGUAGES:
        if code == DEFAULT_LANGUAGE:
            continue
        path = LOCALES_DIR / f"{code}.json"
        old = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        new = {key: old.get(key, "") for key in keys}
        path.write_text(
            json.dumps(new, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        missing = sum(1 for value in new.values() if not value)
        print(f"{code}: строк {len(new)}, без перевода {missing}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
