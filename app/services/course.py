"""Курс на портале: недели, конспекты, практики.

Материалы живут в отдельном репозитории преподавателей. Портал забирает их
сам — архивом по HTTP, раз в несколько часов (`refresh`), — и кладёт в том
данных. Поэтому здесь ни таблиц, ни загрузок через интерфейс: читаем с диска
то, что уже приехало.

Почему в том, а не рядом с кодом: материалы правятся чаще, чем портал, и
пересобирать образ ради новой недели незачем. Заодно чужая авторская работа
не попадает в исходники.

Раскладка повторяет источник: `<корень>/sem<N>/<неделя>/`, в папке недели
обязательный `card.md` с полями в YAML, опциональные ноутбуки фиксированных
имён и любые файлы-приложения.
"""

from __future__ import annotations

import io
import logging
import re
import shutil
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from app.config import settings
from app.i18n import mark as N_
from app.i18n import translate as _

logger = logging.getLogger(__name__)

# Копия в репозитории — для локальной разработки и для стенда, который ещё
# не настроил источник. Том данных главнее: туда приезжают свежие материалы.
REPO_COPY = Path(__file__).resolve().parent.parent.parent / "course" / "algo-1"


def course_root() -> Path:
    volume = settings.data_dir / "course"
    return volume if (volume / "pages").is_dir() or (volume / "sem1").is_dir() else REPO_COPY

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
    path = course_root() / f"sem{semester}" / number
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
    root = course_root() / f"sem{semester}"
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
    path = course_root() / "pages" / f"{name}.md"
    if not path.is_file():
        return None
    fields, body = _parse_front_matter(path.read_text(encoding="utf-8"))
    return fields.get("title", name), body


class CourseSourceError(RuntimeError):
    """Источник материалов недоступен или отдал не то."""


def _strip_top_level(names: list[str]) -> str:
    """Общий верхний каталог архива, если он один.

    Gitea и GitHub кладут содержимое репозитория в папку вида `algo-1/`.
    Нам нужно то, что внутри, иначе недели окажутся уровнем ниже.
    """
    tops = {name.split("/", 1)[0] for name in names if name and not name.startswith("/")}
    return tops.pop() + "/" if len(tops) == 1 else ""


def unpack(archive: bytes, target: Path) -> int:
    """Распаковывает архив курса в каталог, подменяя его целиком.

    Подмена атомарная: сначала собираем рядом, потом переименовываем. Иначе
    студент, открывший страницу в момент обновления, увидит полкурса.

    Возвращает число распакованных файлов.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=target.parent, prefix=".course-new-"))
    previous = target.with_name(target.name + ".old")
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            names = tar.getnames()
            prefix = _strip_top_level(names)
            # filter="data" отсекает ссылки наружу, абсолютные пути и «..» —
            # архив приезжает по сети, доверять ему нечего.
            tar.extractall(staging, filter="data")

        root = staging / prefix if prefix else staging
        if not root.is_dir():
            raise CourseSourceError("в архиве нет ожидаемого каталога")

        if target.exists():
            target.rename(previous)
        root.rename(target)
        return sum(1 for _ in target.rglob("*") if _.is_file())
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(previous, ignore_errors=True)


async def download(url: str, token: str = "") -> bytes:
    """Скачивает архив курса. Токен нужен приватному репозиторию."""
    headers = {"Authorization": f"token {token}"} if token else {}
    try:
        async with httpx.AsyncClient(timeout=settings.course_timeout, follow_redirects=True) as c:
            response = await c.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise CourseSourceError(f"источник недоступен: {exc}") from exc
    if response.status_code != 200:
        # 404 у Gitea означает и «нет репозитория», и «нет доступа»: приватный
        # репозиторий не признаётся в своём существовании.
        raise CourseSourceError(f"источник ответил {response.status_code}")
    return response.content


async def refresh() -> int:
    """Забирает материалы из источника в том данных. Возвращает число файлов."""
    if not settings.course_source_url:
        return 0
    archive = await download(settings.course_source_url, settings.course_token)
    files = unpack(archive, settings.data_dir / "course")
    logger.info("материалы курса обновлены: файлов %s", files)
    return files


def is_available() -> bool:
    """Без выгруженного курса пункт меню не показываем."""
    return course_root().is_dir()
