from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Значение из .env.example. Стартовать с ним в проде нельзя: подпись сессионных
# кук предсказуема, и любой может войти под чужим аккаунтом.
INSECURE_SECRET = "dev-insecure-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Портал тренировок по программированию"
    app_short_name: str = "Тренировки"
    # Чьи студенты учатся на портале. Подставляется в тексты про подтверждение
    # учётной записи: «студент МГУ», «учётная запись факультета». Пусто —
    # тексты становятся общими, без упоминания конкретного вуза.
    org_name: str = ""
    org_account_name: str = "учётная запись организации"
    # Название курса в разделе «Курс». Пусто — заголовком станет слово «Курс»:
    # у каждого вуза курс свой, а в исходнике чужое название ни к чему.
    course_title: str = ""
    course_subtitle: str = ""
    # Откуда портал забирает материалы курса: ссылка на архив ветки.
    # У Gitea и GitHub это /api/v1/repos/<owner>/<repo>/archive/<ветка>.tar.gz.
    # Пусто — не забираем вовсе, показываем то, что уже лежит в томе.
    course_source_url: str = ""
    # Токен для приватного репозитория с материалами.
    course_token: str = ""
    course_refresh_hours: int = 6
    course_timeout: float = 60.0
    # Сервер проверки решений. Поднимается на время контрольной, адрес обычно
    # голый IP: http://10.0.0.5:2358. Пусто — портал принимает код, но не
    # проверяет его и говорит об этом прямо.
    judge0_url: str = ""
    judge0_token: str = ""
    judge0_language_id: int = 71  # Python 3 в стандартной сборке judge0
    judge0_timeout: float = 30.0
    # Общий срок на прогон по всем тестам: дальше честнее сказать «не успели»,
    # чем держать студента и соединение к судье.
    judge0_total_timeout: float = 120.0
    # Сколько раз студент может сдать одну задачу контрольной.
    task_attempts: int = 5
    # Язык портала для тех, кто ничего не выбрал: ru, en или fr. Выбор человека
    # и подсказку браузера он не перебивает — только замыкает цепочку.
    default_language: str = "ru"
    display_timezone: str = "Europe/Moscow"
    base_url: str = "http://localhost:8000"
    secret_key: str = INSECURE_SECRET
    # Осознанное разрешение работать с дефолтным ключом (тесты, разовая проверка).
    allow_insecure_secret: bool = False
    # Токен для загрузки исходников посылок скриптом с машины преподавателя.
    # Пусто — приём выключен: лучше не иметь ручки вовсе, чем открытую.
    ingest_token: str = ""
    data_dir: Path = Path("./data")

    # Telegram
    telegram_bot_token: str = ""
    telegram_bot_username: str = ""
    # Telegram id преподавателей через запятую. Id не меняется, в отличие от логина.
    teacher_telegram_ids: str = ""
    # Общий чат или канал для уведомлений (id вида -1001234567890). Пусто — не слать.
    telegram_notify_chat_id: str = ""
    # За сколько минут до старта события напоминать.
    reminder_minutes_before: int = 60
    # За сколько часов до дедлайна задания напомнить тем, кто не закрыл.
    assignment_reminder_hours: int = 24

    # OpenID Connect (Authentik, Keycloak и любой другой провайдер с discovery).
    oidc_issuer: str = ""            # https://auth.example.org/application/o/sport/
    oidc_client_id: str = ""
    oidc_client_secret: str = ""     # пусто — публичный клиент, только PKCE
    oidc_scopes: str = "openid profile email"
    oidc_provider_name: str = "Authentik"
    oidc_groups_claim: str = "groups"
    oidc_teacher_groups: str = ""    # группы провайдера, дающие роль преподавателя

    # Локальная разработка без Telegram.
    dev_login_enabled: bool = False

    # Фоновые задачи
    enable_scheduler: bool = True
    enable_bot: bool = True
    sync_interval_seconds: int = 600
    sync_stale_seconds: int = 540
    catalog_refresh_hours: int = 24

    # Клиенты платформ
    codeforces_min_interval: float = 2.1
    leetcode_min_interval: float = 1.0
    leetcode_user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    http_timeout: float = 30.0

    # Сессии и коды
    login_code_ttl_seconds: int = 600
    session_ttl_seconds: int = 60 * 60 * 24 * 30
    session_cookie: str = "sport_session"

    @property
    def org_student(self) -> str:
        """«студент МГУ» или просто «студент», если организация не названа."""
        return f"студент {self.org_name}" if self.org_name.strip() else "студент"

    @property
    def telegram_enabled(self) -> bool:
        """Без токена и имени бота ни входа через бота, ни уведомлений нет."""
        return bool(self.telegram_bot_token.strip() and self.telegram_bot_username.strip())

    @property
    def oidc_enabled(self) -> bool:
        return bool(self.oidc_issuer.strip() and self.oidc_client_id.strip())

    @property
    def oidc_teacher_group_set(self) -> set[str]:
        raw = self.oidc_teacher_groups.replace(";", ",").split(",")
        return {g.strip() for g in raw if g.strip()}

    @property
    def oidc_redirect_uri(self) -> str:
        return self.base_url.rstrip("/") + "/login/oidc/callback"

    @property
    def secret_is_insecure(self) -> bool:
        return self.secret_key.strip() in {"", INSECURE_SECRET}

    @property
    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.data_dir.resolve() / 'sport.db'}"

    @property
    def teacher_ids(self) -> set[int]:
        out: set[int] = set()
        for chunk in self.teacher_telegram_ids.replace(";", ",").split(","):
            chunk = chunk.strip()
            if chunk:
                try:
                    out.add(int(chunk))
                except ValueError:
                    continue
        return out


    @property
    def telegram_login_url_template(self) -> str:
        return f"https://t.me/{self.telegram_bot_username}?start={{code}}"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings


settings = get_settings()
