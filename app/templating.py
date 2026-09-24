from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates

from app.config import settings
from app.i18n import LANGUAGES, current_language
from app.i18n import mark as N_
from app.i18n import translate as _
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


# Логотип портала. Сначала смотрим в DATA_DIR/branding — туда учреждение кладёт
# свой знак, не трогая исходники и не пересобирая образ. Потом в app/static
# с нейтральным значком по умолчанию. SVG предпочтительнее PNG: не мылится.
LOGO_NAMES = ("logo.svg", "logo.png", "logo.webp")
LOGO_DARK_NAMES = ("logo-dark.svg", "logo-dark.png", "logo-dark.webp")
FAVICON_NAMES = ("favicon.png", "favicon.svg", "favicon.ico", *LOGO_NAMES)
# Компактный знак для рейки. Рисуется CSS-маской в цвет текста рейки — цвет файла не важен.
MARK_NAMES = ("mark.svg", "mark.png")


BRANDING_DIR = settings.data_dir / "branding"


def _find_asset(names: tuple[str, ...]) -> str | None:
    """Адрес знака: сначала фирменный стиль учреждения, затем запасной."""
    for name in names:
        if (BRANDING_DIR / name).is_file():
            return f"/branding/{name}"
    for name in names:
        if (STATIC_DIR / name).is_file():
            return f"/static/{name}"
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


def plural(count: int, forms: str) -> str:
    """Форма слова по числу: «день|дня|дней», «day|days».

    Сколько форм в строке — столько знает язык перевода: три у русского,
    две у английского и французского. Правило выбирается по текущему языку,
    а не по числу форм, потому что ноль во французском ещё единственное число.
    """
    parts = forms.split("|")
    if len(parts) >= 3:
        return plural_ru(count, parts[0], parts[1], parts[2])
    if len(parts) < 2:
        return parts[0]
    single = abs(count) < 2 if current_language() == "fr" else abs(count) == 1
    return parts[0] if single else parts[1]


def timeago(value: datetime | None, now: datetime | None = None) -> str:
    """`now` подставляется в тестах: иначе результат зависит от часа запуска."""
    if value is None:
        return "—"
    now = now or datetime.now(UTC)
    seconds = (now - value).total_seconds()

    if seconds < 0:
        return fmt_dt(value)
    if seconds < 60:
        return _("только что")
    # «сколько-то назад» собирается целой фразой: во французском это
    # «il y a 5 minutes» — слово «назад» стоит впереди, а не сзади.
    if seconds < 3600:
        minutes = int(seconds // 60)
        return _("%(count)s %(unit)s назад") % {
            "count": minutes,
            "unit": plural(minutes, _("минуту|минуты|минут")),
        }
    if seconds < 86400:
        hours = int(seconds // 3600)
        return _("%(count)s %(unit)s назад") % {
            "count": hours,
            "unit": plural(hours, _("час|часа|часов")),
        }

    local = value.astimezone(LOCAL_TZ)
    today = now.astimezone(LOCAL_TZ).date()
    days = (today - local.date()).days
    if days == 1:
        return _("вчера в %(time)s") % {"time": f"{local:%H:%M}"}
    if days < 7:
        return _("%(count)s %(unit)s назад") % {
            "count": days,
            "unit": plural(days, _("день|дня|дней")),
        }
    if local.year == today.year:
        return _("%(date)s в %(time)s") % {"date": f"{local:%d.%m}", "time": f"{local:%H:%M}"}
    return f"{local:%d.%m.%Y}"


def sentence(text: str | None) -> str:
    """Заглавная только первая буква; остальное как есть.

    Не `capitalize`: тот опускает хвост, и «учётная запись МГУ» превращается
    в «Учётная запись мгу». Аббревиатуры в названиях вузов — обычное дело.
    """
    value = str(text or "")
    return value[:1].upper() + value[1:]


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


# Русские месяцы в родительном падеже — «12 января». В переводе стоят
# именительные: «January», «janvier», — порядок слов задаёт формат ниже.
MONTHS = (
    N_("января"), N_("февраля"), N_("марта"), N_("апреля"), N_("мая"), N_("июня"),
    N_("июля"), N_("августа"), N_("сентября"), N_("октября"), N_("ноября"), N_("декабря"),
)


def day_label(value: datetime) -> str:
    local = value.astimezone(LOCAL_TZ)
    today = datetime.now(LOCAL_TZ).date()
    delta = (today - local.date()).days
    if delta == 0:
        return _("Сегодня")
    if delta == 1:
        return _("Вчера")
    label = _("%(day)s %(month)s") % {"day": local.day, "month": _(MONTHS[local.month - 1])}
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
templates.env.filters["sentence"] = sentence
templates.env.filters["hue"] = avatar_hue
templates.env.filters["plural"] = plural
templates.env.filters["gravatar"] = gravatar_url
# Макрос иконок доступен во всех шаблонах без ручного import — спрайт
# при этом выводится один раз через {% include "_icons.html" %} в base.html.
# Цветовые темы: ключ совпадает с data-theme в style.css. Названы по краске,
# а не по устройству («светлая», «тёмная»): выбирают ведь не яркость экрана,
# а сочетание. Порядок — порядок в меню.
THEMES: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (N_("Светлые"), (
        ("amber", N_("Янтарь")),
        ("parchment", N_("Пергамент")),
        ("mandarin", N_("Мандарин")),
        ("lime", N_("Лайм")),
        ("finland", N_("Финляндия")),
        ("japan", N_("Япония")),
    )),
    (N_("Тёмные"), (
        ("coal", N_("Уголь")),
        ("sweden", N_("Швеция")),
        ("brazil", N_("Бразилия")),
        ("oxford", N_("Оксфорд")),
        ("neon", N_("Неон")),
        ("ice", N_("Лёд")),
        ("magenta", N_("Магента")),
    )),
)

THEME_KEYS: tuple[str, ...] = tuple(key for _group, items in THEMES for key, _label in items)

templates.env.globals["icon"] = templates.env.get_template("_icons.html").module.icon
templates.env.globals["avatar"] = templates.env.get_template("_ui.html").module.avatar
templates.env.globals["problem_link"] = templates.env.get_template("_ui.html").module.problem_link
templates.env.globals["group_by_day"] = group_by_day
templates.env.globals["asset_version"] = asset_version()
templates.env.globals["brand_logo"] = _find_asset(LOGO_NAMES)
templates.env.globals["brand_logo_dark"] = _find_asset(LOGO_DARK_NAMES)
templates.env.globals["favicon_asset"] = _find_asset(FAVICON_NAMES)
templates.env.globals["brand_mark"] = _find_asset(MARK_NAMES)
templates.env.globals["course_available"] = course.is_available()
# Перевод: строка в шаблоне — русская, она же ключ словаря.
def js_strings() -> dict[str, str]:
    """Переводы для скриптов: инлайнового JS не держим, поэтому отдаём данными."""
    return {
        "countdown.before_start": _("до старта"),
        "countdown.before_end": _("до конца"),
        "countdown.live": _("идёт сейчас"),
        "countdown.past": _("завершено"),
        # Формы слова через «|»: сколько их — столько знает язык.
        "countdown.days": _("день|дня|дней"),
    }


templates.env.globals["_"] = _
templates.env.globals["js_strings"] = js_strings
templates.env.globals["languages"] = LANGUAGES
templates.env.globals["themes"] = THEMES
templates.env.globals["language"] = current_language
templates.env.globals["settings"] = settings
templates.env.globals["SolveStatus"] = SolveStatus
templates.env.globals["now"] = lambda: datetime.now(UTC)
