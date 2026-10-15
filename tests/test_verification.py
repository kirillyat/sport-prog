from __future__ import annotations

import pytest

from app.models import Platform, PlatformAccount, User, utcnow
from app.platforms.base import RemoteProfile, UserNotFound
from app.services import verification


@pytest.fixture
async def users(session):
    anya, borya = User(display_name="Аня"), User(display_name="Боря")
    session.add_all([anya, borya])
    await session.commit()
    return anya, borya


def _profile(text: str) -> RemoteProfile:
    return RemoteProfile(handle="anya", display_name="Аня", searchable_text=text)


async def test_start_verification_issues_code(session, users, monkeypatch):
    anya, _ = users

    async def fake_fetch(platform, handle):
        return _profile("")

    monkeypatch.setattr(verification, "fetch_profile", fake_fetch)
    account = await verification.start_verification(session, anya, Platform.codeforces, " tourist ")

    assert account.handle == "tourist"  # пробелы срезаются
    assert account.verification_code.startswith("algo-")
    assert account.is_verified is False


async def test_unknown_handle_rejected_early(session, users, monkeypatch):
    anya, _ = users

    async def fake_fetch(platform, handle):
        raise UserNotFound(handle)

    monkeypatch.setattr(verification, "fetch_profile", fake_fetch)
    with pytest.raises(ValueError, match="нет пользователя"):
        await verification.start_verification(session, anya, Platform.codeforces, "опечатка")


async def test_confirm_requires_code_in_profile(session, users, monkeypatch):
    anya, _ = users

    async def empty(platform, handle):
        return _profile("ITMO University")

    monkeypatch.setattr(verification, "fetch_profile", empty)
    account = await verification.start_verification(session, anya, Platform.leetcode, "anya")
    assert await verification.confirm_verification(session, account) is False
    assert account.is_verified is False

    code = account.verification_code

    async def with_code(platform, handle):
        return _profile(f"МГУ, {code.upper()}")  # регистр не важен

    monkeypatch.setattr(verification, "fetch_profile", with_code)
    assert await verification.confirm_verification(session, account) is True
    assert account.is_verified is True
    assert account.verification_code is None


async def test_handle_cannot_be_claimed_twice(session, users, monkeypatch):
    anya, borya = users

    async def fake_fetch(platform, handle):
        return _profile("")

    monkeypatch.setattr(verification, "fetch_profile", fake_fetch)
    account = await verification.start_verification(session, anya, Platform.codeforces, "tourist")
    account.verified_at = utcnow()
    await session.commit()

    with pytest.raises(ValueError, match="уже привязан"):
        await verification.start_verification(session, borya, Platform.codeforces, "tourist")


async def test_relinking_resets_verification(session, users, monkeypatch):
    anya, _ = users

    async def fake_fetch(platform, handle):
        return _profile("")

    monkeypatch.setattr(verification, "fetch_profile", fake_fetch)
    account = await verification.start_verification(session, anya, Platform.codeforces, "old")
    account.verified_at = utcnow()
    await session.commit()

    account = await verification.start_verification(session, anya, Platform.codeforces, "new")
    assert account.handle == "new"
    assert account.is_verified is False
    assert account.verification_code is not None

    # У пользователя по-прежнему ровно один аккаунт на платформу.
    from sqlalchemy import func, select
    count = await session.scalar(
        select(func.count()).select_from(PlatformAccount).where(PlatformAccount.user_id == anya.id)
    )
    assert count == 1


async def test_code_issued_before_the_prefix_change_still_confirms(session, users, monkeypatch):
    """Приставку сменили с «msu-sport-» на нейтральную. Студент, который уже
    вписал старый код в профиль, не должен получать отказ."""
    anya, _ = users
    account = PlatformAccount(
        user_id=anya.id, platform=Platform.codeforces, handle="anya",
        verification_code="msu-sport-1a2b3c4d",
    )
    session.add(account)
    await session.commit()

    async def with_old_code(platform, handle):
        return _profile("organization: msu-sport-1a2b3c4d")

    monkeypatch.setattr(verification, "fetch_profile", with_old_code)
    assert await verification.confirm_verification(session, account) is True


def test_prefix_does_not_name_a_university():
    """Код видно всем, кто откроет профиль студента на площадке."""
    assert verification.new_verification_code().startswith("algo-")
