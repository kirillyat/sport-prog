"""Видимость разделов портала: флаги для студентов и преподавателей отдельно.

Зачем: раздел бывает готов у преподавателя раньше, чем у студентов, — курс
выложен наполовину, материалы ещё правятся. Вместо выкладки по кускам
преподаватель закрывает раздел студентам и открывает, когда готов.

Флага в базе нет — раздел открыт всем. Поэтому новый стенд поднимается
в полном составе, а таблица наполняется только осознанными запретами.
Исключение — раздел, который ещё обкатывается: у него своё `default`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import SessionLocal
from app.i18n import mark as N_
from app.models import FeatureFlag, ReviewStatus, SolutionUpload, User, utcnow
from app.security import read_session


@dataclass(frozen=True, slots=True)
class Section:
    key: str
    title: str
    path: str
    hint: str
    # До какого дня в рейке висит плашка «новое». Дата, а не флаг в базе:
    # плашка снимается сама, и никому не нужно помнить, что её пора убрать.
    new_until: date | None = None
    # Что действует, пока преподаватель ничего не сохранял: (студентам,
    # преподавателям). Почти всегда «открыто всем», см. докстроку модуля.
    default: tuple[bool, bool] = (True, True)

    def is_new(self, today: date) -> bool:
        return self.new_until is not None and today < self.new_until


# Что вообще можно закрыть. Добавить раздел — добавить строку сюда.
SECTIONS: tuple[Section, ...] = (
    Section("materials", N_("Материалы"), "/materials",
            N_("Ноутбуки с семинаров, конспекты и разборы, которые выкладывает преподаватель")),
    Section("course", N_("Курс"), "/course",
            N_("Недели курса с конспектами, практиками и домашними заданиями")),
    Section("grades", N_("Оценки"), "/grades",
            N_("Своя строка ведомости: посещаемость, оценки за работы и комментарии")),
    Section("ai", N_("ИИ"), "/ai",
            N_("Подключение нейросети к порталу по MCP. Закрытый раздел "
               "отключает и ключи этой роли"),
            new_until=date(2026, 10, 22)),
    # Единственный раздел, закрытый студентам с рождения: он ещё
    # обкатывается, и сам по себе на проде открыться не должен.
    Section("contest", N_("Соревнования"), "/contest",
            N_("Командные и личные соревнования по правилам ICPC и IOI: регистрация "
               "команд, старт по кнопке, своё табло"),
            default=(False, True)),
)

SECTION_BY_KEY = {section.key: section for section in SECTIONS}


def default(key: str) -> tuple[bool, bool]:
    """Видимость раздела, про который в базе ничего не сказано."""
    section = SECTION_BY_KEY.get(key)
    return section.default if section else (True, True)


Flags = dict[str, tuple[bool, bool]]


async def load(session: AsyncSession) -> Flags:
    rows = (await session.execute(select(FeatureFlag))).scalars().all()
    return {row.key: (row.for_students, row.for_teachers) for row in rows}


async def save(session: AsyncSession, key: str, *, for_students: bool, for_teachers: bool) -> None:
    if key not in SECTION_BY_KEY:
        return
    stmt = sqlite_insert(FeatureFlag).values(
        key=key, for_students=for_students, for_teachers=for_teachers, updated_at=utcnow()
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[FeatureFlag.key],
        set_={
            "for_students": stmt.excluded.for_students,
            "for_teachers": stmt.excluded.for_teachers,
            "updated_at": stmt.excluded.updated_at,
        },
    )
    await session.execute(stmt)
    await session.commit()


def allows(flags: Flags, key: str, user: User | None) -> bool:
    """Открыт ли раздел этому человеку. Роль решает, какой из двух флагов смотреть."""
    if key not in SECTION_BY_KEY:
        return True
    if user is None:
        return False
    return allows_role(flags, key, teacher=user.is_teacher)


def allows_role(flags: Flags, key: str, *, teacher: bool) -> bool:
    """То же для роли — когда смотрят «глазами студента», а человек преподаватель."""
    if key not in SECTION_BY_KEY:
        return True
    for_students, for_teachers = flags.get(key) or default(key)
    return for_teachers if teacher else for_students


def visible(flags: Flags, user: User | None) -> set[str]:
    return {section.key for section in SECTIONS if allows(flags, section.key, user)}


# --- то же самое, но для каждой страницы ------------------------------------
#
# Рейка слева рисуется в base.html, то есть флаги нужны любому шаблону. Берём
# их в middleware — как и бегущую строку, — и кладём в контекст. Запрос к SQLite
# тут копеечный: в таблице столько строк, сколько закрытых разделов.

SKIP_PREFIXES = ("/static", "/healthz", "/login", "/logout")


EMPTY_NAV: dict = {
    "sections_on": set(),
    "student_closed": set(),
    "pending_reviews": 0,
    "is_teacher": False,
}


async def load_for_request(request) -> dict:
    """Что нужно рейке на каждой странице: видимые разделы и счётчик проверки."""
    if request.method != "GET" or request.url.path.startswith(SKIP_PREFIXES):
        return dict(EMPTY_NAV)
    token = request.cookies.get(settings.session_cookie)
    user_id = read_session(token) if token else None
    if user_id is None:
        return dict(EMPTY_NAV)

    async with SessionLocal() as session:
        user = await session.get(User, user_id)
        if user is None or not user.is_active:
            return dict(EMPTY_NAV)

        flags = await load(session)
        pending = 0
        if user.is_teacher:
            pending = await session.scalar(
                select(func.count())
                .select_from(SolutionUpload)
                .where(SolutionUpload.status == ReviewStatus.pending)
            )
        return {
            "sections_on": visible(flags, user),
            # Что закрыто студентам: преподаватель видит эти пункты пунктиром
            # и не гадает, открыт ли раздел. Студенту знать это незачем.
            "student_closed": {
                section.key for section in SECTIONS
                if not allows_role(flags, section.key, teacher=False)
            } if user.is_teacher else set(),
            "pending_reviews": int(pending or 0),
            "is_teacher": user.is_teacher,
        }


def features_context(request) -> dict:
    """Общий контекст шаблонов: что показывать и чьими глазами."""
    nav = getattr(request.state, "nav", None) or EMPTY_NAV
    as_student = request.cookies.get("view_as") == "student"
    # Шаблоны импортируют этот модуль, поэтому пояс берём у них только здесь.
    from app.templating import LOCAL_TZ

    today = utcnow().astimezone(LOCAL_TZ).date()
    return {
        "sections_on": nav["sections_on"],
        # Плашка «новое» — только у открытых разделов: у закрытого её некуда вешать.
        "sections_new": {
            section.key for section in SECTIONS
            if section.key in nav["sections_on"] and section.is_new(today)
        },
        "pending_reviews": nav.get("pending_reviews", 0),
        # Глазами студента пунктира нет: студент его тоже не видит.
        "student_closed": set() if as_student else nav.get("student_closed", set()),
        # Раздел текущей страницы, если студентам он закрыт: преподаватель
        # видит над страницей пунктирную полосу и не гадает, видна ли она группе.
        "page_closed": None if as_student else next(
            (
                section
                for section in SECTIONS
                if section.key in nav.get("student_closed", set())
                and (request.url.path == section.path
                     or request.url.path.startswith(section.path + "/"))
            ),
            None,
        ),
        # Читаем куку прямо здесь: это взгляд, а не данные, запрос к базе не нужен.
        "as_student": as_student,
        # «Преподаватель» для показа: он сам может попросить показать портал
        # глазами студента, и тогда преподавательских кнопок быть не должно.
        "teacher_view": nav.get("is_teacher", False) and not as_student,
        # Настройки портала — только во включённом режиме администратора.
        # Права проверяет require_admin, здесь — только что рисовать.
        "admin_view": nav.get("is_teacher", False)
        and not as_student
        and request.cookies.get("admin_mode") == "on",
    }
