from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class UtcDateTime(TypeDecorator):
    """SQLite не хранит таймзону. Пишем всегда UTC и возвращаем tz-aware datetime.

    Без этого сравнения `submitted_at > assigned_at` начинают падать на
    naive/aware и тихо ломать подсчёт «решено в срок».
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("в базу должен уходить только tz-aware datetime")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class EnumStr(TypeDecorator):
    """Хранит enum строкой и возвращает именно enum, а не str.

    Без этого объект, только что созданный в Python, держит enum, а тот же
    объект, прочитанный из базы, — строку. Половина кода начинает падать
    на `.value` и `.title` в самых неожиданных местах.
    """

    impl = String
    cache_ok = True

    def __init__(self, enum_cls, length: int = 16) -> None:
        super().__init__(length)
        self._enum = enum_cls

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return self._enum(value).value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return self._enum(value)


class Base(DeclarativeBase):
    type_annotation_map = {datetime: UtcDateTime}


class Role(enum.StrEnum):
    student = "student"
    teacher = "teacher"


class Platform(enum.StrEnum):
    leetcode = "leetcode"
    codeforces = "codeforces"

    @property
    def title(self) -> str:
        return {"leetcode": "LeetCode", "codeforces": "Codeforces"}[self.value]


class SolveStatus(enum.StrEnum):
    not_solved = "not_solved"
    solved_in_time = "solved_in_time"
    solved_late = "solved_late"          # после мягкого дедлайна: половина баллов
    solved_too_late = "solved_too_late"  # после жёсткого дедлайна: не засчитываем
    solved_before = "solved_before"      # решена до выдачи задания — не засчитываем


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    display_name: Mapped[str] = mapped_column(String(120))
    role: Mapped[Role] = mapped_column(EnumStr(Role), default=Role.student)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    # Telegram-аккаунт ровно один на пользователя, поэтому держим инлайном.
    telegram_id: Mapped[int | None] = mapped_column(Integer, unique=True, index=True)
    telegram_username: Mapped[str | None] = mapped_column(String(64))

    # Идентификатор пользователя у OIDC-провайдера (Authentik). Стабилен, в отличие от email.
    oidc_sub: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255))

    # Почта для Gravatar — отдельно от рабочей: аватарка часто заведена на личную.
    # Пусто — аватарки нет, рисуем инициалы и наружу ничего не ходит.
    gravatar_email: Mapped[str | None] = mapped_column(String(255))

    accounts: Mapped[list[PlatformAccount]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )
    memberships: Mapped[list[GroupMembership]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def is_teacher(self) -> bool:
        return self.role == Role.teacher

    def account_for(self, platform: Platform) -> PlatformAccount | None:
        for acc in self.accounts:
            if acc.platform == platform:
                return acc
        return None


class PlatformAccount(Base):
    __tablename__ = "platform_accounts"
    __table_args__ = (
        UniqueConstraint("user_id", "platform", name="uq_account_user_platform"),
        Index("ix_account_platform_handle", "platform", "handle"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    platform: Mapped[Platform] = mapped_column(EnumStr(Platform))
    handle: Mapped[str] = mapped_column(String(120))

    verification_code: Mapped[str | None] = mapped_column(String(64))
    verified_at: Mapped[datetime | None] = mapped_column()

    last_synced_at: Mapped[datetime | None] = mapped_column()
    last_sync_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    user: Mapped[User] = relationship(back_populates="accounts")

    @property
    def is_verified(self) -> bool:
        return self.verified_at is not None

    @property
    def profile_url(self) -> str:
        if self.platform == Platform.codeforces:
            return f"https://codeforces.com/profile/{self.handle}"
        return f"https://leetcode.com/u/{self.handle}/"


class Problem(Base):
    __tablename__ = "problems"
    __table_args__ = (
        UniqueConstraint("platform", "external_id", name="uq_problem_platform_external"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[Platform] = mapped_column(EnumStr(Platform), index=True)
    external_id: Mapped[str] = mapped_column(String(64))
    slug: Mapped[str] = mapped_column(String(200), index=True)
    title: Mapped[str] = mapped_column(String(300))
    url: Mapped[str] = mapped_column(String(400))
    difficulty: Mapped[str | None] = mapped_column(String(16))  # LeetCode: Easy/Medium/Hard
    rating: Mapped[int | None] = mapped_column(Integer)  # Codeforces
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    is_paid_only: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    @property
    def label(self) -> str:
        if self.platform == Platform.codeforces:
            return f"CF {self.external_id}"
        return f"LC {self.external_id}"

    @property
    def weight_hint(self) -> str:
        if self.difficulty:
            return self.difficulty
        if self.rating:
            return str(self.rating)
        return "—"


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    join_code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)
    # Telegram-чат группы для уведомлений. Пусто — уходит в общий чат.
    telegram_chat_id: Mapped[str | None] = mapped_column(String(32))

    memberships: Mapped[list[GroupMembership]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )


class GroupMembership(Base):
    __tablename__ = "group_memberships"
    __table_args__ = (UniqueConstraint("group_id", "user_id", name="uq_membership"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    joined_at: Mapped[datetime] = mapped_column(default=utcnow)

    group: Mapped[Group] = relationship(back_populates="memberships")
    user: Mapped[User] = relationship(back_populates="memberships")


class FeatureFlag(Base):
    """Видимость раздела портала: отдельно студентам, отдельно преподавателям.

    Строки нет — раздел открыт всем: пустая таблица означает портал в полном
    составе, и это правильное поведение по умолчанию для нового стенда.
    """

    __tablename__ = "feature_flags"

    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    for_students: Mapped[bool] = mapped_column(Boolean, default=True)
    for_teachers: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class ProblemSet(Base):
    __tablename__ = "problem_sets"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    items: Mapped[list[ProblemSetItem]] = relationship(
        back_populates="problem_set",
        cascade="all, delete-orphan",
        order_by="ProblemSetItem.position",
        lazy="selectin",
    )


class ProblemSetItem(Base):
    __tablename__ = "problem_set_items"
    __table_args__ = (UniqueConstraint("problem_set_id", "problem_id", name="uq_set_problem"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    problem_set_id: Mapped[int] = mapped_column(ForeignKey("problem_sets.id", ondelete="CASCADE"))
    problem_id: Mapped[int] = mapped_column(ForeignKey("problems.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer, default=0)
    weight: Mapped[float] = mapped_column(Float, default=1.0)

    problem_set: Mapped[ProblemSet] = relationship(back_populates="items")
    problem: Mapped[Problem] = relationship(lazy="selectin")


class Assignment(Base):
    __tablename__ = "assignments"
    __table_args__ = (
        # Либо группа, либо один студент, либо (оба NULL) все сразу.
        CheckConstraint(
            "group_id IS NULL OR user_id IS NULL",
            name="ck_assignment_single_target",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    problem_set_id: Mapped[int] = mapped_column(ForeignKey("problem_sets.id", ondelete="CASCADE"))
    # Обе ссылки пусты — задание для всех.
    group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    assigned_at: Mapped[datetime] = mapped_column(default=utcnow)
    deadline: Mapped[datetime | None] = mapped_column()
    # Жёсткий дедлайн: после срока решение не засчитывается вовсе. Так делаются марафоны.
    hard_deadline: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # Засчитывать решения, сделанные ДО выдачи задания. По умолчанию нет.
    count_prior_solves: Mapped[bool] = mapped_column(Boolean, default=False)
    # Когда напомнили о дедлайне. Пусто — ещё не напоминали.
    reminded_at: Mapped[datetime | None] = mapped_column()

    problem_set: Mapped[ProblemSet] = relationship(lazy="selectin")
    group: Mapped[Group | None] = relationship(lazy="selectin")


class Announcement(Base):
    """Объявление: анонс контеста, сбор, организационная новость."""

    __tablename__ = "announcements"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str | None] = mapped_column(Text)
    # Ссылка на соревнование или регистрацию.
    url: Mapped[str | None] = mapped_column(String(500))
    url_label: Mapped[str | None] = mapped_column(String(80))
    # NULL — объявление для всех, иначе только для этой группы.
    group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"))
    # К чему ведём обратный отсчёт и когда всё закончится.
    starts_at: Mapped[datetime | None] = mapped_column()
    ends_at: Mapped[datetime | None] = mapped_column()
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    # Когда ушло напоминание о старте, чтобы не слать дважды.
    reminded_at: Mapped[datetime | None] = mapped_column()

    group: Mapped[Group | None] = relationship(lazy="selectin")

    def state(self, now: datetime) -> str:
        """upcoming — ещё не началось, live — идёт, past — прошло, plain — без времени."""
        if self.starts_at is None:
            return "plain"
        if now < self.starts_at:
            return "upcoming"
        if self.ends_at is not None and now > self.ends_at:
            return "past"
        if self.ends_at is None and (now - self.starts_at).total_seconds() > 86400:
            return "past"
        return "live"


class Submission(Base):
    __tablename__ = "submissions"
    __table_args__ = (
        UniqueConstraint("platform", "external_id", name="uq_submission_platform_external"),
        Index("ix_submission_user_problem", "user_id", "problem_id", "is_accepted"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    platform_account_id: Mapped[int] = mapped_column(
        ForeignKey("platform_accounts.id", ondelete="CASCADE")
    )
    platform: Mapped[Platform] = mapped_column(EnumStr(Platform))
    external_id: Mapped[str] = mapped_column(String(64))
    # NULL, если задачи нет в нашем каталоге (новый контест, задача из gym).
    problem_id: Mapped[int | None] = mapped_column(ForeignKey("problems.id", ondelete="SET NULL"))
    problem_slug: Mapped[str | None] = mapped_column(String(200))
    problem_title: Mapped[str | None] = mapped_column(String(300))
    verdict: Mapped[str | None] = mapped_column(String(48))
    is_accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    language: Mapped[str | None] = mapped_column(String(80))
    submitted_at: Mapped[datetime] = mapped_column(index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    problem: Mapped[Problem | None] = relationship(lazy="selectin")


class Material(Base):
    """Материал курса: ноутбук с семинара, конспект, презентация.

    Сам файл лежит на диске рядом с базой — в SQLite его класть незачем.
    В таблице только то, по чему ищут и показывают.
    """

    __tablename__ = "materials"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    # Пусто — материал для всех.
    group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"))
    # Имя на диске — случайное: пользовательское в путь не попадает никогда.
    stored_name: Mapped[str] = mapped_column(String(80), unique=True)
    # Имя, под которым файл скачается.
    filename: Mapped[str] = mapped_column(String(200))
    content_type: Mapped[str] = mapped_column(String(120))
    size: Mapped[int] = mapped_column(Integer)
    published_at: Mapped[datetime] = mapped_column(default=utcnow)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    group: Mapped[Group | None] = relationship(lazy="selectin")

    @property
    def can_preview(self) -> bool:
        from app.services.notebook import is_previewable

        return is_previewable(self.filename, self.size)

    @property
    def size_label(self) -> str:
        if self.size < 1024:
            return f"{self.size} Б"
        if self.size < 1024 * 1024:
            return f"{self.size / 1024:.0f} КБ"
        return f"{self.size / 1024 / 1024:.1f} МБ"


class BonusPoint(Base):
    """Ручная корректировка баллов преподавателем."""

    __tablename__ = "bonus_points"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    points: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(String(300))
    granted_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    granted_at: Mapped[datetime] = mapped_column(default=utcnow)


class LoginToken(Base):
    """Одноразовый код для входа через Telegram-бота."""

    __tablename__ = "login_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime] = mapped_column()
    telegram_id: Mapped[int | None] = mapped_column(Integer)
    telegram_username: Mapped[str | None] = mapped_column(String(64))
    display_name: Mapped[str | None] = mapped_column(String(120))
    confirmed_at: Mapped[datetime | None] = mapped_column()
    consumed_at: Mapped[datetime | None] = mapped_column()
    # Если задано — это не вход, а привязка Telegram к уже существующей учётке.
    # SQLite пересоздаёт таблицу при ALTER, поэтому имя ограничения задаём явно.
    link_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE", name="fk_login_token_link_user")
    )


class SyncState(Base):
    """Служебные отметки: когда в последний раз обновляли каталог задач."""

    __tablename__ = "sync_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
