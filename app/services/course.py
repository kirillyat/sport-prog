"""Курс «Алгоритмы и структуры данных» на портале.

Материалы курса живут в отдельном репозитории преподавателей и приезжают к нам
скриптом `scripts/sync_course.py` — в папку `course/`. Поэтому здесь ни таблиц,
ни загрузок через интерфейс: читаем с диска то, что уже лежит рядом с кодом.

Раскладка повторяет источник: `course/<курс>/sem<N>/<неделя>/`, в папке недели
обязательный `card.md` с полями в YAML, опциональные ноутбуки фиксированных
имён и любые файлы-приложения.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from app.i18n import mark as N_
from app.i18n import translate as _

COURSE_ROOT = Path(__file__).resolve().parent.parent.parent / "course" / "algo-1"

# Имена фиксированы в репозитории курса: нет файла — нет слота на странице.
SLOTS = (
    ("lecture", N_("конспект")),
    ("seminar", N_("практика")),
    ("hw-lecture", N_("ДЗ лекции")),
    ("hw-seminar", N_("ДЗ семинара")),
)

WEEK_RE = re.compile(r"^\d{2}$")
SEMESTER_RE = re.compile(r"^[12]$")
FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)


@dataclass(slots=True)
class Slot:
    key: str
    label: str
    filename: str


@dataclass(slots=True)
class Attachment:
    name: str
    size: int


@dataclass(slots=True)
class Week:
    semester: str          # «1» — как в адресе, а не «sem1»
    number: str            # «01»
    title: str
    lecture_title: str
    seminar_title: str
    body: str
    slots: list[Slot] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"/course/{self.semester}/{self.number}"


def _parse_front_matter(raw: str) -> tuple[dict[str, str], str]:
    """Разбираем шапку card.md.

    Полноценный YAML тут не нужен и не заводится ради трёх строк вида
    `title: "Неделя 2"` — лишняя зависимость дороже, чем этот разбор.
    """
    match = FRONT_MATTER.match(raw)
    if not match:
        return {}, raw.strip()
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        fields[key.strip()] = value.strip().strip('"').strip("'")
    return fields, match.group(2).strip()


def _week_dir(semester: str, number: str) -> Path | None:
    """Путь к папке недели. Номера проверяем регуляркой, а не склейкой строк."""
    if not SEMESTER_RE.match(semester) or not WEEK_RE.match(number):
        return None
    path = COURSE_ROOT / f"sem{semester}" / number
    return path if (path / "card.md").is_file() else None


def _load(semester: str, number: str, path: Path) -> Week:
    fields, body = _parse_front_matter((path / "card.md").read_text(encoding="utf-8"))
    slots = [
        Slot(key, label, f"{key}.ipynb")
        for key, label in SLOTS
        if (path / f"{key}.ipynb").is_file()
    ]
    taken = {"card.md", *(slot.filename for slot in slots)}
    attachments = [
        Attachment(item.name, item.stat().st_size)
        for item in sorted(path.iterdir())
        if item.is_file() and item.name not in taken
    ]
    return Week(
        semester=semester,
        number=number,
        title=fields.get("title") or _("Неделя %(number)s") % {"number": number.lstrip("0")},
        lecture_title=fields.get("lecture_title", ""),
        seminar_title=fields.get("seminar_title", ""),
        body=body,
        slots=slots,
        attachments=attachments,
    )


def weeks(semester: str = "1") -> list[Week]:
    root = COURSE_ROOT / f"sem{semester}"
    if not root.is_dir():
        return []
    found = []
    for path in sorted(root.iterdir()):
        if path.is_dir() and WEEK_RE.match(path.name) and (path / "card.md").is_file():
            found.append(_load(semester, path.name, path))
    return found


def week(semester: str, number: str) -> Week | None:
    path = _week_dir(semester, number)
    return _load(semester, number, path) if path else None


def file_path(semester: str, number: str, name: str) -> Path | None:
    """Файл недели по имени. Имя берём только как имя — каталоги из него выкидываем."""
    path = _week_dir(semester, number)
    if path is None:
        return None
    candidate = path / Path(name).name
    return candidate if candidate.is_file() else None


def page(name: str) -> tuple[str, str] | None:
    """Статическая страница курса: «О курсе», «Авторы». Отдаём заголовок и текст."""
    if name not in {"about", "authors"}:
        return None
    path = COURSE_ROOT / "pages" / f"{name}.md"
    if not path.is_file():
        return None
    fields, body = _parse_front_matter(path.read_text(encoding="utf-8"))
    return fields.get("title", name), body


def is_available() -> bool:
    """Без выгруженного курса пункт меню не показываем."""
    return COURSE_ROOT.is_dir()
