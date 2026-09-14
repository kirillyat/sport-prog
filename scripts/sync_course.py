#!/usr/bin/env python3
"""Забирает материалы курса из репозитория преподавателей в наш `course/`.

Сервер не видит git.ai.msu.ru (NAT заворачивает имя на шлюз), поэтому портал
не тянет курс сам: контент лежит в репозитории и уезжает на сервер тем же
пушем, что и код. Пересинхронизировать — запустить этот скрипт на клон.

    python scripts/sync_course.py ~/src/algo-1

Берём только то, что курс сам считает публичным: папки недель и страницы.
Планы пар, эталонные решения и генератор сайта остаются у преподавателей.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

WEEK_DIRS = ("sem1", "sem2")
PAGES = ("about.md", "authors.md")
SKIP = {".DS_Store"}


def sync(source: Path, target: Path) -> None:
    if not (source / "sem1").is_dir():
        raise SystemExit(f"не похоже на репозиторий курса: {source}")

    if target.exists():
        shutil.rmtree(target)
    (target / "pages").mkdir(parents=True)

    weeks = 0
    files = 0
    for semester in WEEK_DIRS:
        if not (source / semester).is_dir():
            continue
        for week in sorted((source / semester).iterdir()):
            if not week.is_dir() or not (week / "card.md").is_file():
                continue
            destination = target / semester / week.name
            destination.mkdir(parents=True)
            for item in sorted(week.iterdir()):
                if item.is_file() and item.name not in SKIP:
                    shutil.copy2(item, destination / item.name)
                    files += 1
            weeks += 1

    for page in PAGES:
        origin = source / "pages" / page
        if origin.is_file():
            shutil.copy2(origin, target / "pages" / page)
            files += 1

    print(f"недель: {weeks}, файлов: {files} → {target}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("укажи путь к клону репозитория курса")
    root = Path(__file__).resolve().parent.parent
    sync(Path(sys.argv[1]).expanduser().resolve(), root / "course" / "algo-1")
