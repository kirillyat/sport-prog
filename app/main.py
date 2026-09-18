from __future__ import annotations

import asyncio
import contextlib
import logging
import mimetypes
from collections.abc import AsyncIterator
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.bot import run_bot
from app.config import settings
from app.deps import Forbidden, RedirectToLogin, section_required
from app.i18n import LANGUAGE_COOKIE, pick_language, set_language
from app.routers import (
    accounts,
    announcements,
    auth,
    course,
    feed,
    ingest,
    leaderboard,
    materials,
    solutions,
    student,
    teacher,
    view,
)
from app.scheduler import run_scheduler
from app.services import features
from app.templating import STATIC_DIR, templates
from app.ticker import load_ticker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
# httpx на INFO печатает полный URL запроса, а у Telegram API токен бота —
# часть пути. Иначе он лежал бы в открытом виде в `docker compose logs`.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("app")

SECRET_HELP = """
SECRET_KEY не задан или оставлен дефолтным.

Этим ключом подписываются сессионные куки: с известным значением любой
может подделать вход под чужим аккаунтом.

Сгенерируй ключ и положи его в .env:

    python -m app.cli gen-secret

Если это разовый локальный запуск и риск понятен:

    ALLOW_INSECURE_SECRET=true
"""


def check_startup_config() -> None:
    if settings.secret_is_insecure and not settings.allow_insecure_secret:
        raise RuntimeError(SECRET_HELP)
    if settings.dev_login_enabled:
        logger.warning("DEV_LOGIN_ENABLED=true — вход без Telegram открыт, не для прода")
    if settings.allow_insecure_secret and settings.secret_is_insecure:
        logger.warning("ALLOW_INSECURE_SECRET=true — сессии подделываются, только для отладки")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    check_startup_config()
    stop_event = asyncio.Event()
    tasks: list[asyncio.Task] = []

    if settings.enable_scheduler:
        tasks.append(asyncio.create_task(run_scheduler(stop_event), name="scheduler"))
    if settings.enable_bot and settings.telegram_bot_token:
        tasks.append(asyncio.create_task(run_bot(stop_event), name="telegram-bot"))
    try:
        yield
    finally:
        stop_event.set()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


app = FastAPI(title=settings.app_name, lifespan=lifespan, docs_url=None, redoc_url=None)

# У стандартной таблицы MIME нет woff2 — без этого шрифт уходит как octet-stream.
mimetypes.add_type("font/woff2", ".woff2")

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Фирменный стиль учреждения: логотип и значок кладутся в том, а не в образ,
# поэтому свой знак ставится без пересборки и не попадает в открытый исходник.
BRANDING_DIR = settings.data_dir / "branding"
BRANDING_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/branding", StaticFiles(directory=str(BRANDING_DIR)), name="branding")

app.include_router(auth.router)
app.include_router(student.router)
app.include_router(accounts.router)
app.include_router(announcements.router)
app.include_router(feed.router)
app.include_router(materials.router, dependencies=[Depends(section_required("materials"))])
app.include_router(course.router, dependencies=[Depends(section_required("course"))])
app.include_router(solutions.router)
app.include_router(ingest.router)
app.include_router(view.router)
app.include_router(leaderboard.router)
app.include_router(teacher.router)


@app.middleware("http")
async def attach_ticker(request: Request, call_next):
    """Ближайшее событие для строки над страницей. Один лёгкий запрос к SQLite."""
    try:
        request.state.ticker = await load_ticker(request)
    except Exception:  # строка — украшение, страницу из-за неё не роняем
        logger.exception("не удалось собрать ticker")
        request.state.ticker = None
    return await call_next(request)


@app.middleware("http")
async def attach_features(request: Request, call_next):
    """Какие разделы показывать этому человеку — нужно рейке на каждой странице."""
    try:
        request.state.nav = await features.load_for_request(request)
    except Exception:  # рейка не должна ронять страницу; показываем всё
        logger.exception("не удалось собрать состояние навигации")
        request.state.nav = {
            "sections_on": {section.key for section in features.SECTIONS},
            "pending_reviews": 0,
            "is_teacher": False,
        }
    return await call_next(request)


@app.middleware("http")
async def attach_language(request: Request, call_next):
    """Язык страницы — из куки, при первом заходе подсказывает браузер.

    Стоит снаружи остальных middleware: тикер и рейку тоже надо переводить.
    """
    language = pick_language(
        request.cookies.get(LANGUAGE_COOKIE), request.headers.get("accept-language")
    )
    set_language(language)
    request.state.language = language
    return await call_next(request)


@app.exception_handler(RedirectToLogin)
async def _redirect_to_login(request: Request, exc: RedirectToLogin):
    return RedirectResponse(f"/login?next={quote(exc.next_url)}", status_code=303)


@app.exception_handler(Forbidden)
async def _forbidden(request: Request, exc: Forbidden):
    return templates.TemplateResponse(
        request, "error.html", {"user": exc.user, "message": exc.message}, status_code=403
    )


@app.exception_handler(RequestValidationError)
async def _bad_request(request: Request, exc: RequestValidationError):
    """Сырой JSON от FastAPI в браузере выглядит как поломка сайта.

    Показываем обычную страницу, а подробности пишем в лог: пользователю
    они всё равно ничего не говорят.
    """
    logger.warning("Неверный запрос %s: %s", request.url, exc.errors())
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "user": getattr(request.state, "user", None),
            "title": "Не получилось",
            "message": "Страница получила значение, которого не ждала. "
                       "Вернись на главную и попробуй ещё раз.",
        },
        status_code=400,
    )


@app.get("/healthz", include_in_schema=False)
async def healthz():
    return {"status": "ok"}
