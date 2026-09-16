from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DEV_LOGIN_ENABLED", "true")
os.environ.setdefault("ENABLE_SCHEDULER", "false")
os.environ.setdefault("ENABLE_BOT", "false")

from sqlalchemy import event  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db import configure_connection  # noqa: E402
from app.models import Base  # noqa: E402


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> AsyncIterator[async_sessionmaker]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    # Те же настройки соединения, что и у приложения, иначе тесты проверяют
    # не то поведение: например, юникодный lower().
    event.listen(engine.sync_engine, "connect", configure_connection)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()


@pytest_asyncio.fixture
async def session(db: async_sessionmaker) -> AsyncIterator[AsyncSession]:
    async with db() as s:
        yield s


@pytest_asyncio.fixture
async def client(session: AsyncSession, db: async_sessionmaker, monkeypatch) -> AsyncIterator:
    import httpx

    from app import ticker
    from app.db import get_session
    from app.main import app
    from app.services import features

    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    # Middleware строки событий открывает сессии сам, минуя dependency override.
    monkeypatch.setattr(ticker, "SessionLocal", db)
    monkeypatch.setattr(features, "SessionLocal", db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=True
    ) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# --- OIDC ------------------------------------------------------------------
# Фикстура живёт здесь, а не в tests/test_oidc.py: импортировать её из соседнего
# тестового модуля нельзя — пакета `tests` нет, и на чистой машине это падает.

@pytest.fixture
def provider(monkeypatch):
    """Настроенный OIDC-провайдер и подменённые сетевые вызовы."""
    import base64
    import json

    from app import oidc
    from app.config import settings

    disc = oidc.Discovery(
        issuer="https://auth.test/application/o/sport/",
        authorization_endpoint="https://auth.test/application/o/authorize/",
        token_endpoint="https://auth.test/application/o/token/",
        userinfo_endpoint="https://auth.test/application/o/userinfo/",
    )
    monkeypatch.setattr(settings, "oidc_issuer", disc.issuer)
    monkeypatch.setattr(settings, "oidc_client_id", "sport")
    monkeypatch.setattr(settings, "oidc_teacher_groups", "sp-teachers")
    monkeypatch.setattr(oidc, "_discovery", None)

    calls: dict = {"exchange": [], "profile": {}, "disc": disc}

    def jwt(claims: dict) -> str:
        body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
        return f"h.{body}.s"

    async def fake_discover(force=False):
        return disc

    async def fake_exchange(_disc, code, verifier):
        calls["exchange"].append((code, verifier))
        return {"id_token": jwt(calls["claims"]), "access_token": "at"}

    async def fake_userinfo(_disc, _token):
        return calls["profile"]

    monkeypatch.setattr(oidc, "discover", fake_discover)
    monkeypatch.setattr(oidc, "exchange_code", fake_exchange)
    monkeypatch.setattr(oidc, "fetch_userinfo", fake_userinfo)
    return calls
