from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates

from app.config import settings
from app.models import SolveStatus
from app.services import course

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


def asset_version() -> str:
    """Версия статики по времени правки — чтобы браузер не показывал старый CSS."""
    try:
        newest = max(f.stat().st_mtime for f in STATIC_DIR.rglob("*") if f.is_file())
    except ValueError:
        return "0"
    return str(int(newest))


# Логотип портала: если в app/static/ лежит файл — берём его, иначе рисуем
# встроенный знак. SVG предпочтительнее PNG: он не мылится на ретине.
LOGO_NAMES = ("logo.svg", "logo.png", "logo.webp")
LOGO_DARK_NAMES = ("logo-dark.svg", "logo-dark.png", "logo-dark.webp")
FAVICON_NAMES = ("favicon.png", "favicon.svg", "favicon.ico", *LOGO_NAMES)
# Компактный знак для рейки. Рисуется CSS-маской в цвет текста рейки — цвет файла не важен.
MARK_NAMES = ("mark.svg", "mark.png")


def _find_asset(names: tuple[str, ...]) -> str | None:
    for name in names:
        if (STATIC_DIR / name).is_file():
            return name
    return None
from app.services.features import features_context  # noqa: E402
from app.ticker import ticker_context  # noqa: E402

templates = Jinja2Templates(
    directory=str(TEMPLATES_DIR), context_processors=[ticker_context, features_context]
)

try:
    LOCAL_TZ = ZoneInfo(settings.display_timezone)
except Exception:  # некорректная таймзона не должна ронять приложение
    LOCAL_TZ = UTC


def fmt_dt(value: datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    if value is None:
        return "—"
    return value.astimezone(LOCAL_TZ).strftime(fmt)


def fmt_date(value: datetime | None) -> str:
    return fmt_dt(value, "%d.%m.%Y")


def fmt_points(value: float | None) -> str:
    if value is None:
        return "0"
    return f"{value:g}"


def plural_ru(count: int, one: str, few: str, many: str) -> str:
    """минута / минуты / минут — русские числительные не прощают небрежности."""
    tail = abs(count) % 100
    if 11 <= tail <= 14:
        return many
    tail %= 10
    if tail == 1:
        return one
    if 2 <= tail <= 4:
        return few
    return many


def timeago(value: datetime | None, now: datetime | None = None) -> str:
    """`now` подставляется в тестах: иначе результат зависит от часа запуска."""
    if value is None:
        return "—"
    now = now or datetime.now(UTC)
    seconds = (now - value).total_seconds()

    if seconds < 0:
        return fmt_dt(value)
    if seconds < 60:
        return "только что"
    if seconds < 3600:
        minutes = int(seconds // 60)
        return f"{minutes} {plural_ru(minutes, 'минуту', 'минуты', 'минут')} назад"
    if seconds < 86400:
        hours = int(seconds // 3600)
        return f"{hours} {plural_ru(hours, 'час', 'часа', 'часов')} назад"

    local = value.astimezone(LOCAL_TZ)
    today = now.astimezone(LOCAL_TZ).date()
    days = (today - local.date()).days
    if days == 1:
        return f"вчера в {local:%H:%M}"
    if days < 7:
        return f"{days} {plural_ru(days, 'день', 'дня', 'дней')} назад"
    if local.year == today.year:
        return f"{local:%d.%m} в {local:%H:%M}"
    return f"{local:%d.%m.%Y}"


def initials(name: str | None) -> str:
    if not name:
        return "?"
    parts = [p for p in str(name).split() if p]
    if not parts:
        return "?"
    return "".join(p[0] for p in parts[:2]).upper()


GRAVATAR_BASE = "https://gravatar.com/avatar/"


def gravatar_url(email: str | None, size: int = 128) -> str | None:
    """Ссылка на Gravatar или None, если почта не указана.

    Хеш считается от почты в нижнем регистре без пробелов по краям — так велит
    Gravatar. `d=404` означает «нет картинки — отдай 404»: тогда шаблон покажет
    инициалы вместо чужой заглушки. `r=g` отсекает картинки не для всех.
    """
    clean = (email or "").strip().lower()
    if not clean:
        return None
    digest = hashlib.sha256(clean.encode()).hexdigest()
    return f"{GRAVATAR_BASE}{digest}?s={size}&d=404&r=g"


def avatar_hue(value: object) -> int:
    """Стабильный цвет аватарки по имени — без хранения лишнего поля."""
    text = str(value or "")
    total = 0
    for index, char in enumerate(text):
        total = (total * 31 + ord(char) + index) % 360
    return total


MONTHS_RU = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def day_label(value: datetime) -> str:
    local = value.astimezone(LOCAL_TZ)
    today = datetime.now(LOCAL_TZ).date()
    delta = (today - local.date()).days
    if delta == 0:
        return "Сегодня"
    if delta == 1:
        return "Вчера"
    label = f"{local.day} {MONTHS_RU[local.month - 1]}"
    return label if local.year == today.year else f"{label} {local.year}"


def group_by_day(items) -> list[tuple[str, list]]:
    """[(«Сегодня», [...]), («Вчера», [...])] — порядок элементов сохраняется."""
    grouped: list[tuple[str, list]] = []
    for item in items:
        label = day_label(item.solved_at)
        if grouped and grouped[-1][0] == label:
            grouped[-1][1].append(item)
        else:
            grouped.append((label, [item]))
    return grouped


def to_local_input(value: datetime | None) -> str:
    """Значение для <input type="datetime-local">."""
    if value is None:
        return ""
    return value.astimezone(LOCAL_TZ).strftime("%Y-%m-%dT%H:%M")


def parse_local_input(raw: str | None) -> datetime | None:
    """Читает <input type="datetime-local"> как местное время и переводит в UTC."""
    if not raw:
        return None
    try:
        naive = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return naive.replace(tzinfo=LOCAL_TZ).astimezone(UTC)


templates.env.filters["dt"] = fmt_dt
templates.env.filters["d"] = fmt_date
templates.env.filters["pts"] = fmt_points
templates.env.filters["dtinput"] = to_local_input
templates.env.filters["ago"] = timeago
templates.env.filters["initials"] = initials
templates.env.filters["hue"] = avatar_hue
templates.env.filters["plural"] = plural_ru
templates.env.filters["gravatar"] = gravatar_url
# Макрос иконок доступен во всех шаблонах без ручного import — спрайт
# при этом выводится один раз через {% include "_icons.html" %} в base.html.
templates.env.globals["icon"] = templates.env.get_template("_icons.html").module.icon
templates.env.globals["avatar"] = templates.env.get_template("_ui.html").module.avatar
templates.env.globals["group_by_day"] = group_by_day
templates.env.globals["asset_version"] = asset_version()
templates.env.globals["brand_logo"] = _find_asset(LOGO_NAMES)
templates.env.globals["brand_logo_dark"] = _find_asset(LOGO_DARK_NAMES)
templates.env.globals["favicon_asset"] = _find_asset(FAVICON_NAMES)
templates.env.globals["brand_mark"] = _find_asset(MARK_NAMES)
templates.env.globals["course_available"] = course.is_available()
templates.env.globals["settings"] = settings
templates.env.globals["SolveStatus"] = SolveStatus
templates.env.globals["now"] = lambda: datetime.now(UTC)
