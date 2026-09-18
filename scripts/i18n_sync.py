#!/usr/bin/env python3
"""Собирает строки `_("…")` из шаблонов и кода в словари app/locales/*.json.

Запуск:  python scripts/i18n_sync.py

Новые строки попадают в словарь с пустым значением — их видно и понятно,
что осталось перевести. Уже переведённое не трогается, исчезнувшее из кода
убирается. Порядок ключей — как в исходниках, поэтому diff читаемый.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.i18n import DEFAULT_LANGUAGE, LANGUAGES, LOCALES_DIR  # noqa: E402

SOURCES = [
    (ROOT / "app" / "templates", "*.html"),
    (ROOT / "app", "*.py"),
]

# _("…") и _('…'); внутри допускаем экранированные кавычки.
CALL = re.compile(r"""_\(\s*(?P<q>["'])(?P<text>(?:\\.|(?!(?P=q)).)*)(?P=q)\s*[),]""")


def unescape(raw: str, quote: str) -> str:
    return raw.replace("\\" + quote, quote).replace("\\\\", "\\")


def collect() -> list[str]:
    seen: dict[str, None] = {}
    for base, pattern in SOURCES:
        for path in sorted(base.rglob(pattern)):
            if path.name == "i18n.py" or "locales" in path.parts:
                continue
            for match in CALL.finditer(path.read_text(encoding="utf-8")):
                seen.setdefault(unescape(match.group("text"), match.group("q")), None)
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
