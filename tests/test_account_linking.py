"""Один участник — несколько способов входа.

Без привязки вход через Authentik и вход через Telegram создавали бы
двух разных людей на портале.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.models import LoginToken, User, utcnow


async def _dev_login(client, name, teacher=False):
    data = {"name": name}
    if teacher:
        data["teacher"] = "true"
    await client.post("/login/dev", data=data)


async def _telegram_code(session, client) -> str:
    """Начинаем привязку из кабинета и возвращаем выданный код."""
    response = await client.get("/login/telegram/link")
    assert response.status_code == 200
    token = await session.scalar(
        select(LoginToken).where(LoginToken.link_user_id.is_not(None)).order_by(LoginToken.id.desc())
    )
    assert token is not None
    return token.code


async def _confirm(session, code, telegram_id, username="anya"):
    token = await session.scalar(select(LoginToken).where(LoginToken.code == code))
    token.telegram_id = telegram_id
    token.telegram_username = username
    token.display_name = "Аня"
    token.confirmed_at = utcnow()
    await session.commit()


@pytest.fixture(autouse=True)
def telegram_configured(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "test-token")
    monkeypatch.setattr(settings, "telegram_bot_username", "sport_bot")


async def test_telegram_links_to_existing_account(session, client):
    await _dev_login(client, "Аня", teacher=True)
    user = await session.scalar(select(User).where(User.display_name == "Аня"))
    assert user.telegram_id is None

    code = await _telegram_code(session, client)
    await _confirm(session, code, telegram_id=555)
    response = await client.get(f"/login/complete/{code}")

    assert "Telegram привязан" in response.text
    await session.refresh(user)
    assert user.telegram_id == 555
    assert user.telegram_username == "anya"
    # Новой учётки не появилось.
    assert await session.scalar(select(func.count()).select_from(User)) == 1


async def test_telegram_login_after_linking_lands_in_same_account(session, client):
    await _dev_login(client, "Аня", teacher=True)
    code = await _telegram_code(session, client)
    await _confirm(session, code, telegram_id=555)
    await client.get(f"/login/complete/{code}")
    await client.post("/logout")

    # Обычный вход через бота тем же telegram_id — это тот же человек.
    page = await client.get("/login")
    login_code = await session.scalar(select(LoginToken).order_by(LoginToken.id.desc()))
    assert page.status_code == 200
    await _confirm(session, login_code.code, telegram_id=555)
    await client.get(f"/login/complete/{login_code.code}")

    assert await session.scalar(select(func.count()).select_from(User)) == 1
    profile = await client.get("/me")
    assert "Аня" in profile.text


async def test_telegram_already_used_by_someone_else(session, client):
    other = User(display_name="Боря", telegram_id=555)
    session.add(other)
    await session.commit()

    await _dev_login(client, "Аня", teacher=True)
    code = await _telegram_code(session, client)
    await _confirm(session, code, telegram_id=555)
    response = await client.get(f"/login/complete/{code}")

    assert "уже привязан к другому участнику" in response.text
    anya = await session.scalar(select(User).where(User.display_name == "Аня"))
    assert anya.telegram_id is None


async def test_link_code_useless_in_another_browser(session, client, db):
    """Чужой код не должен цеплять Telegram к учётке, которая его не запрашивала."""
    import httpx

    await _dev_login(client, "Аня", teacher=True)
    code = await _telegram_code(session, client)
    await _confirm(session, code, telegram_id=555)

    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                 follow_redirects=True) as stranger:
        response = await stranger.get(f"/login/complete/{code}")

    assert "начни привязку заново" in response.text
    anya = await session.scalar(select(User).where(User.display_name == "Аня"))
    assert anya.telegram_id is None


async def test_oidc_links_to_existing_account(session, client, provider):
    await _dev_login(client, "Аня", teacher=True)
    user = await session.scalar(select(User).where(User.display_name == "Аня"))

    start = await client.get("/login/oidc?link=1", follow_redirects=False)
    assert start.status_code == 303
    from urllib.parse import parse_qs, urlparse
    query = parse_qs(urlparse(start.headers["location"]).query)
    provider["claims"] = {"sub": "ak-77", "nonce": query["nonce"][0], "email": "anya@msu.ru"}
    provider["profile"] = {"groups": []}

    response = await client.get(f"/login/oidc/callback?code=c&state={query['state'][0]}")
    assert "привязан" in response.text
    await session.refresh(user)
    assert user.oidc_sub == "ak-77"
    assert user.email == "anya@msu.ru"
    assert await session.scalar(select(func.count()).select_from(User)) == 1


async def test_telegram_stays_until_the_university_account_is_linked(session, client):
    await _dev_login(client, "Аня", teacher=True)
    user = await session.scalar(select(User).where(User.display_name == "Аня"))
    user.telegram_id = 555
    await session.commit()

    response = await client.post("/accounts/telegram/unlink")
    assert "входить будет нечем" in response.text
    await session.refresh(user)
    assert user.telegram_id == 555


async def test_university_account_cannot_be_unlinked(session, client):
    """Она и есть подтверждение студенчества: отвязали бы — человек остался
    бы в группах, перестав быть подтверждённым."""
    await _dev_login(client, "Аня", teacher=True)
    user = await session.scalar(select(User).where(User.display_name == "Аня"))
    user.telegram_id = 555
    user.oidc_sub = "ak-1"
    await session.commit()

    response = await client.post("/accounts/oidc/unlink")
    assert "отвязать нельзя" in response.text
    await session.refresh(user)
    assert user.oidc_sub == "ak-1"


async def test_unlink_works_when_another_method_remains(session, client):
    await _dev_login(client, "Аня", teacher=True)
    user = await session.scalar(select(User).where(User.display_name == "Аня"))
    user.telegram_id = 555
    user.oidc_sub = "ak-1"
    await session.commit()

    response = await client.post("/accounts/telegram/unlink")
    assert "Telegram отвязан" in response.text
    await session.refresh(user)
    assert user.telegram_id is None
    assert user.oidc_sub == "ak-1"


async def test_accounts_page_shows_both_methods(session, client):
    await _dev_login(client, "Аня", teacher=True)
    page = await client.get("/accounts")
    assert "Способы входа" in page.text
    assert "/login/telegram/link" in page.text


async def test_unconfirmed_user_cannot_join_a_group(session, client, monkeypatch):
    """Telegram подтверждает только Telegram. Группа — после учётной записи вуза."""
    from app.config import settings
    from app.models import Group, GroupMembership, Role

    monkeypatch.setattr(settings, "oidc_issuer", "https://id.example/application/o/sp/")
    monkeypatch.setattr(settings, "oidc_client_id", "portal")

    await _dev_login(client, "Аня")
    user = await session.scalar(select(User).where(User.display_name == "Аня"))
    user.role = Role.student
    user.telegram_id = 555
    group = Group(title="Осень", join_code="JOIN01")
    session.add(group)
    await session.commit()

    denied = await client.post("/groups/join", data={"join_code": "JOIN01"})
    assert "подтверди студенчество" in denied.text.lower()
    assert await session.scalar(select(func.count()).select_from(GroupMembership)) == 0

    user.oidc_sub = "ak-9"
    await session.commit()
    allowed = await client.post("/groups/join", data={"join_code": "JOIN01"})
    assert "Ты в группе" in allowed.text
    assert await session.scalar(select(func.count()).select_from(GroupMembership)) == 1


async def test_unconfirmed_user_is_not_a_club_member(session, client, monkeypatch):
    """Ни на табло, ни в общем задании: студенчество ещё не подтверждено."""
    from app.config import settings
    from app.models import Assignment, ProblemSet, Role
    from app.services.leaderboard import build_leaderboard
    from app.services.progress import participants_for_assignment

    monkeypatch.setattr(settings, "oidc_issuer", "https://id.example/application/o/sp/")
    monkeypatch.setattr(settings, "oidc_client_id", "portal")

    confirmed = User(display_name="Аня", role=Role.student, oidc_sub="ak-1")
    guest = User(display_name="Гость", role=Role.student, telegram_id=777)
    problem_set = ProblemSet(title="Список")
    session.add_all([confirmed, guest, problem_set])
    await session.commit()
    assignment = Assignment(title="Всем", problem_set_id=problem_set.id)
    session.add(assignment)
    await session.commit()

    assert [u.display_name for u in await participants_for_assignment(session, assignment)] == [
        "Аня"
    ]
    rows = await build_leaderboard(session)
    assert [r.user.display_name for r in rows] == ["Аня"]
