from __future__ import annotations

import hashlib

import httpx
from sqlalchemy import select

from app.models import Group, GroupMembership, Platform, Problem, Role, User
from app.templating import gravatar_url


async def _login(client: httpx.AsyncClient, name: str, teacher: bool = False):
    data = {"name": name}
    if teacher:
        data["teacher"] = "true"
    return await client.post("/login/dev", data=data)


async def test_anonymous_is_sent_to_login(session, client):
    response = await client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


async def test_dev_login_creates_user_with_role(session, client):
    await _login(client, "Кирилл", teacher=True)
    user = await session.scalar(select(User).where(User.display_name == "Кирилл"))
    assert user.role == Role.teacher

    response = await client.get("/teacher")
    assert response.status_code == 200
    assert "Панель преподавателя" in response.text


async def test_first_user_becomes_teacher(session, client):
    """Иначе портал запирается: сменить роль можно только со страницы для преподавателя."""
    await _login(client, "Аня")  # без галочки «преподаватель»
    user = await session.scalar(select(User).where(User.display_name == "Аня"))
    assert user.role == Role.teacher

    await client.post("/logout")
    await _login(client, "Боря")
    second = await session.scalar(select(User).where(User.display_name == "Боря"))
    assert second.role == Role.student


async def test_student_cannot_open_teacher_pages(session, client):
    await _login(client, "Кирилл", teacher=True)  # занимаем место первого
    await client.post("/logout")
    await _login(client, "Аня")

    response = await client.get("/teacher/assignments")
    assert response.status_code == 403
    assert "только для преподавателя" in response.text
    # Навигация на странице ошибки сохраняется — иначе пользователь в тупике.
    assert "Лидерборд" in response.text


async def test_logout_clears_session(session, client):
    await _login(client, "Аня")
    assert (await client.get("/")).status_code == 200
    await client.post("/logout")
    response = await client.get("/", follow_redirects=False)
    assert response.status_code == 303


async def test_teacher_creates_group_and_student_joins(session, client):
    await _login(client, "Кирилл", teacher=True)
    await client.post("/teacher/groups", data={"title": "Алгоритмы"})
    group = await session.scalar(select(Group))
    assert group is not None and len(group.join_code) == 6

    await client.post("/logout")
    await _login(client, "Аня")
    response = await client.post("/groups/join", data={"join_code": group.join_code})
    assert "Ты в группе" in response.text
    assert "Алгоритмы" in (await client.get("/")).text


async def test_join_with_wrong_code_is_rejected(session, client):
    await _login(client, "Аня")
    response = await client.post("/groups/join", data={"join_code": "НЕТУ1"})
    assert "не найден" in response.text


async def test_full_teacher_flow_renders_matrix(session, client):
    session.add_all([
        Problem(platform=Platform.codeforces, external_id="4A", slug="4a", title="Watermelon",
                url="https://codeforces.com/problemset/problem/4/A", rating=800),
        Problem(platform=Platform.leetcode, external_id="1", slug="two-sum", title="Two Sum",
                url="https://leetcode.com/problems/two-sum/", difficulty="Easy"),
    ])
    await session.commit()

    await _login(client, "Кирилл", teacher=True)
    await client.post("/teacher/groups", data={"title": "Алгоритмы"})
    group = await session.scalar(select(Group))

    created = await client.post("/teacher/sets", data={
        "title": "Разминка", "description": "",
        "problems_text": "cf:4A\nlc:two-sum\nчего-то-нет",
    })
    assert "Не распознано" in created.text  # про несуществующую строку сказали честно
    assert "Watermelon" in created.text and "Two Sum" in created.text

    from app.models import ProblemSet
    problem_set = await session.scalar(select(ProblemSet))
    response = await client.post("/teacher/assignments", data={
        "title": "Неделя 1", "problem_set_id": problem_set.id,
        "group_id": group.id, "deadline": "",
    })
    assert "Неделя 1" in response.text
    assert "В группе нет студентов" in response.text


async def test_assignment_hidden_from_outsiders(session, client):
    from app.models import Assignment, ProblemSet

    problem_set = ProblemSet(title="Список")
    session.add(problem_set)
    await session.commit()
    owner = User(display_name="Аня")
    session.add(owner)
    await session.commit()
    assignment = Assignment(title="Личное", problem_set_id=problem_set.id, user_id=owner.id)
    session.add(assignment)
    await session.commit()

    await _login(client, "Боря")
    response = await client.get(f"/assignments/{assignment.id}")
    assert "не для тебя" in response.text


async def test_dev_login_disabled_by_default(session, client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "dev_login_enabled", False)
    response = await client.post("/login/dev", data={"name": "Хакер"})
    assert "выключен" in response.text
    assert await session.scalar(select(User).where(User.display_name == "Хакер")) is None


async def test_healthz(session, client):
    assert (await client.get("/healthz")).json() == {"status": "ok"}


async def test_profile_sync_button_updates_own_results(session, client, monkeypatch):
    from datetime import UTC, datetime

    from app.models import Platform, PlatformAccount, Role
    from app.routers import student as student_router

    # Аккаунт заводим до входа: тестовая сессия одна на всё, и коллекция
    # accounts у уже загруженного пользователя не обновится сама.
    user = User(display_name="Аня", role=Role.teacher)
    session.add(user)
    await session.commit()
    session.add(PlatformAccount(user_id=user.id, platform=Platform.leetcode,
                                handle="anya", verified_at=datetime.now(UTC)))
    await session.commit()
    await _login(client, "Аня")

    called = []

    async def fake_sync(_session, account):
        called.append(account.handle)
        account.last_sync_error = None
        return 3

    monkeypatch.setattr(student_router, "sync_account", fake_sync)
    response = await client.post(f"/u/{user.id}/sync")
    assert called == ["anya"]
    assert "Обновлено: 3 новых решения" in response.text


async def test_profile_sync_requires_verified_account(session, client):
    await _login(client, "Аня", teacher=True)
    user = await session.scalar(select(User).where(User.display_name == "Аня"))
    response = await client.post(f"/u/{user.id}/sync")
    assert "Нет подтверждённых аккаунтов" in response.text


async def test_student_cannot_sync_someone_else(session, client):
    await _login(client, "Кирилл", teacher=True)
    await client.post("/logout")
    await _login(client, "Аня")
    await client.post("/logout")
    await _login(client, "Боря")

    anya = await session.scalar(select(User).where(User.display_name == "Аня"))
    response = await client.post(f"/u/{anya.id}/sync")
    assert "Чужой профиль обновить нельзя" in response.text


def test_startup_refuses_default_secret(monkeypatch):
    """Дефолтный ключ в проде = подделываемые сессии. Лучше не стартовать вовсе."""
    import pytest

    from app.config import INSECURE_SECRET, settings
    from app.main import check_startup_config

    monkeypatch.setattr(settings, "secret_key", INSECURE_SECRET)
    monkeypatch.setattr(settings, "allow_insecure_secret", False)
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        check_startup_config()


def test_startup_allows_default_secret_when_explicitly_permitted(monkeypatch):
    from app.config import INSECURE_SECRET, settings
    from app.main import check_startup_config

    monkeypatch.setattr(settings, "secret_key", INSECURE_SECRET)
    monkeypatch.setattr(settings, "allow_insecure_secret", True)
    check_startup_config()


def test_startup_accepts_real_secret(monkeypatch):
    from app.config import settings
    from app.main import check_startup_config

    monkeypatch.setattr(settings, "secret_key", "a-real-generated-key")
    monkeypatch.setattr(settings, "allow_insecure_secret", False)
    check_startup_config()


async def test_login_page_wires_telegram_script(session, client, monkeypatch):
    """Вкладка t.me закрывается скриптом — разметка для него должна быть на месте."""
    from app.config import settings

    monkeypatch.setattr(settings, "telegram_bot_token", "t")
    monkeypatch.setattr(settings, "telegram_bot_username", "sport_bot")

    page = await client.get("/login")
    assert "data-tg-code=" in page.text
    assert "data-tg-link" in page.text
    assert "telegram-login.js" in page.text
    # Инлайновых скриптов на странице не осталось — логика в одном файле.
    assert "<script>" not in page.text.split("</head>", 1)[1]


async def test_user_renames_self(session, client):
    await _login(client, "Аня", teacher=True)
    user = await session.scalar(select(User).where(User.display_name == "Аня"))

    response = await client.post(f"/u/{user.id}/name", data={"display_name": "  Аня  Ковалёва "})
    assert "Имя изменено" in response.text
    await session.refresh(user)
    # Лишние пробелы схлопываются, иначе в таблицах появляются «разные» люди.
    assert user.display_name == "Аня Ковалёва"


async def test_empty_name_rejected(session, client):
    await _login(client, "Аня", teacher=True)
    user = await session.scalar(select(User).where(User.display_name == "Аня"))
    response = await client.post(f"/u/{user.id}/name", data={"display_name": "   "})
    assert "не может быть пустым" in response.text
    await session.refresh(user)
    assert user.display_name == "Аня"


async def test_duplicate_name_rejected(session, client):
    await _login(client, "Кирилл", teacher=True)
    await client.post("/logout")
    await _login(client, "Аня")
    anya = await session.scalar(select(User).where(User.display_name == "Аня"))

    response = await client.post(f"/u/{anya.id}/name", data={"display_name": "кирилл"})
    assert "уже занято" in response.text
    await session.refresh(anya)
    assert anya.display_name == "Аня"


async def test_student_cannot_rename_someone_else(session, client):
    await _login(client, "Кирилл", teacher=True)
    await client.post("/logout")
    await _login(client, "Аня")
    await client.post("/logout")
    await _login(client, "Боря")

    anya = await session.scalar(select(User).where(User.display_name == "Аня"))
    response = await client.post(f"/u/{anya.id}/name", data={"display_name": "Взломано"})
    assert "Чужое имя менять нельзя" in response.text
    await session.refresh(anya)
    assert anya.display_name == "Аня"


async def test_teacher_renames_student(session, client):
    await _login(client, "Кирилл", teacher=True)
    await client.post("/logout")
    await _login(client, "kira_2007")
    await client.post("/logout")
    await _login(client, "Кирилл")

    student = await session.scalar(select(User).where(User.display_name == "kira_2007"))
    response = await client.post(f"/u/{student.id}/name", data={"display_name": "Кира Соколова"})
    assert "Имя изменено" in response.text
    await session.refresh(student)
    assert student.display_name == "Кира Соколова"


async def test_rename_form_hidden_from_outsiders(session, client):
    await _login(client, "Кирилл", teacher=True)
    await client.post("/logout")
    await _login(client, "Аня")
    await client.post("/logout")
    await _login(client, "Боря")

    anya = await session.scalar(select(User).where(User.display_name == "Аня"))
    page = await client.get(f"/u/{anya.id}")
    assert f'action="/u/{anya.id}/name"' not in page.text


async def test_assignment_form_creates_marathon(session, client):
    """Марафон теперь создаётся как задание с жёстким дедлайном и плоскими баллами."""
    from app.models import Assignment, ProblemSet

    await _login(client, "Кирилл", teacher=True)
    problem_set = ProblemSet(title="Список")
    session.add(problem_set)
    await session.commit()

    response = await client.post("/teacher/assignments", data={
        "title": "Субботний марафон", "problem_set_id": problem_set.id,
        "group_id": "", "starts_at": "2026-12-06T12:00", "deadline": "2026-12-06T20:00",
        "hard_deadline": "true",
    })
    assert "Задание выдано" in response.text

    item = await session.scalar(select(Assignment))
    assert item.group_id is None and item.user_id is None      # всем
    assert item.hard_deadline is True
    assert item.deadline is not None


async def test_hard_deadline_requires_a_date(session, client):
    from app.models import ProblemSet

    await _login(client, "Кирилл", teacher=True)
    problem_set = ProblemSet(title="Список")
    session.add(problem_set)
    await session.commit()

    response = await client.post("/teacher/assignments", data={
        "title": "Без даты", "problem_set_id": problem_set.id,
        "group_id": "", "hard_deadline": "true",
    })
    assert "без даты не работает" in response.text


async def test_only_title_and_description_are_editable(session, client):
    """Сроки после выдачи не меняются — иначе рейтинг поедет задним числом."""
    from app.models import Assignment, ProblemSet

    await _login(client, "Кирилл", teacher=True)
    problem_set = ProblemSet(title="Список")
    session.add(problem_set)
    await session.commit()
    await client.post("/teacher/assignments", data={
        "title": "Неделя 1", "problem_set_id": problem_set.id, "group_id": "",
        "deadline": "2026-12-01T18:00",
    })
    item = await session.scalar(select(Assignment))
    deadline_before = item.deadline

    response = await client.post(f"/teacher/assignments/{item.id}/edit", data={
        "title": "Неделя 1 — бинпоиск", "description": "Разбор в понедельник",
        # Это поле маршрут не принимает вовсе.
        "deadline": "2027-01-01T00:00",
    })
    assert "Сохранено" in response.text
    await session.refresh(item)
    assert item.title == "Неделя 1 — бинпоиск"
    assert item.description == "Разбор в понедельник"
    assert item.deadline == deadline_before


async def test_gravatar_url_is_sha256_of_trimmed_lowercased_email():
    url = gravatar_url("  Kirill@Example.COM ")
    # Хеш считается от "kirill@example.com": регистр и пробелы Gravatar не прощает.
    digest = hashlib.sha256(b"kirill@example.com").hexdigest()
    assert url == f"https://gravatar.com/avatar/{digest}?s=128&d=404&r=g"
    assert gravatar_url("") is None
    assert gravatar_url(None) is None


async def test_student_sets_and_clears_own_gravatar(session, client):
    await _login(client, "Аня")
    user = await session.scalar(select(User).where(User.display_name == "Аня"))

    await client.post(f"/u/{user.id}/gravatar", data={"gravatar_email": " Me@Example.com "})
    await session.refresh(user)
    assert user.gravatar_email == "Me@Example.com"
    assert "gravatar.com/avatar/" in (await client.get("/me")).text

    await client.post(f"/u/{user.id}/gravatar", data={"gravatar_email": ""})
    await session.refresh(user)
    assert user.gravatar_email is None
    assert "gravatar.com/avatar/" not in (await client.get("/me")).text


async def test_gravatar_rejects_junk_and_other_people(session, client):
    await _login(client, "Аня")
    user = await session.scalar(select(User).where(User.display_name == "Аня"))
    other = User(display_name="Чужой")
    session.add(other)
    await session.commit()

    await client.post(f"/u/{user.id}/gravatar", data={"gravatar_email": "не почта"})
    await session.refresh(user)
    assert user.gravatar_email is None

    # Преподаватель тоже не трогает чужую почту: аватарка — дело личное.
    await client.post(f"/u/{other.id}/gravatar", data={"gravatar_email": "me@example.com"})
    await session.refresh(other)
    assert other.gravatar_email is None


async def test_leaderboard_shows_podium_and_first_solver(session, client):
    from datetime import UTC, datetime, timedelta

    from app.models import (
        Assignment,
        GroupMembership,
        PlatformAccount,
        ProblemSet,
        ProblemSetItem,
        Submission,
    )

    await _login(client, "Кирилл", teacher=True)
    group = Group(title="Осень", join_code="POD123")
    # Преподаватель в рейтинге не участвует, поэтому студентов нужно трое.
    others = [User(display_name=name) for name in ("Аня", "Боря", "Вика")]
    problem = Problem(platform=Platform.leetcode, external_id="1", slug="two-sum",
                      title="Two Sum", url="https://leetcode.com/problems/two-sum/",
                      difficulty="Easy")
    problem_set = ProblemSet(title="Список")
    session.add_all([group, problem, problem_set, *others])
    await session.commit()

    when = datetime.now(UTC) - timedelta(hours=1)
    session.add(ProblemSetItem(problem_set_id=problem_set.id, problem_id=problem.id, position=0))
    session.add(Assignment(title="Неделя 1", problem_set_id=problem_set.id,
                           group_id=group.id, assigned_at=when - timedelta(hours=1)))
    for person in others:
        session.add(GroupMembership(group_id=group.id, user_id=person.id))
    account = PlatformAccount(user_id=others[0].id, platform=Platform.leetcode,
                              handle="anya", verified_at=when)
    session.add(account)
    await session.commit()
    session.add(Submission(
        user_id=others[0].id, platform_account_id=account.id, platform=Platform.leetcode,
        external_id="s1", problem_id=problem.id, problem_slug=problem.slug,
        verdict="Accepted", is_accepted=True, submitted_at=when,
    ))
    await session.commit()

    board = (await client.get("/leaderboard")).text
    assert "podium" in board                 # трое участников — подиум показан
    assert "Аня" in board and "1</b>" in board

    page = (await client.get("/teacher/assignments/1")).text
    assert "Первым" in page and "Аня" in page


async def test_filters_accept_the_all_option(session, client):
    """Пустой group_id из <select> — это «все». Раньше страница падала с 422."""
    await _login(client, "Аня")

    for path in ("/leaderboard?group_id=&period=all", "/feed?group_id="):
        response = await client.get(path)
        assert response.status_code == 200, path


async def test_bonus_form_survives_clumsy_input(session, client):
    """Пустое поле и запятая — обычный ввод, а не 422 с сырым JSON."""
    from app.models import BonusPoint

    await _login(client, "Кирилл", teacher=True)
    student = User(display_name="Аня")
    session.add(student)
    await session.commit()

    empty = await client.post(f"/teacher/students/{student.id}/bonus",
                              data={"points": "", "reason": "за что"})
    assert "это число" in empty.text

    comma = await client.post(f"/teacher/students/{student.id}/bonus",
                              data={"points": "2,5", "reason": ""})
    assert "Баллы начислены" in comma.text
    row = await session.scalar(select(BonusPoint))
    assert row.points == 2.5
    assert row.reason == "без комментария"   # не «без+комментария» из адресной строки


async def test_bad_parameter_shows_a_page_not_json(session, client):
    await _login(client, "Аня")
    response = await client.get("/u/не-число")
    assert response.status_code == 400
    assert "Не получилось" in response.text
    assert "detail" not in response.text


async def test_login_page_without_bot_is_silent_about_telegram(session, client, monkeypatch):
    """Незаполненный .env — забота админа, студенту на странице входа он ни к чему."""
    from app.config import settings

    monkeypatch.setattr(settings, "telegram_bot_token", "")
    monkeypatch.setattr(settings, "telegram_bot_username", "")

    page = await client.get("/login")
    assert "Telegram" not in page.text
    assert "TELEGRAM_BOT_TOKEN" not in page.text


async def test_accounts_page_without_bot_hides_telegram(session, client, monkeypatch):
    """Кнопка «Привязать» вела бы на редирект с ошибкой — значит, её быть не должно."""
    from app.config import settings

    monkeypatch.setattr(settings, "telegram_bot_token", "")
    monkeypatch.setattr(settings, "telegram_bot_username", "")

    await _login(client, "Аня", teacher=True)
    page = await client.get("/accounts")
    assert "Способы входа" in page.text
    assert "/login/telegram/link" not in page.text


async def test_student_can_be_in_several_groups(session, client):
    """Кодов может быть два: членство не заменяется, а добавляется."""
    await _login(client, "Кирилл", teacher=True)
    await client.post("/teacher/groups", data={"title": "Алгоритмы"})
    await client.post("/teacher/groups", data={"title": "Структуры"})
    groups = list((await session.execute(select(Group).order_by(Group.title))).scalars())
    await client.post("/logout")

    await _login(client, "Аня")
    for group in groups:
        await client.post("/groups/join", data={"join_code": group.join_code})

    anya = await session.scalar(select(User).where(User.display_name == "Аня"))
    mine = list(
        (
            await session.execute(
                select(GroupMembership).where(GroupMembership.user_id == anya.id)
            )
        ).scalars()
    )
    assert len(mine) == 2
    page = (await client.get("/")).text
    assert "Алгоритмы" in page and "Структуры" in page


async def test_teacher_removes_student_from_one_group_only(session, client):
    """Исключение из группы не выкидывает студента из остальных и не трогает решения."""
    await _login(client, "Кирилл", teacher=True)
    await client.post("/teacher/groups", data={"title": "Алгоритмы"})
    await client.post("/teacher/groups", data={"title": "Структуры"})
    algo, structures = list(
        (await session.execute(select(Group).order_by(Group.title))).scalars()
    )
    await client.post("/logout")

    await _login(client, "Аня")
    await client.post("/groups/join", data={"join_code": algo.join_code})
    await client.post("/groups/join", data={"join_code": structures.join_code})
    anya = await session.scalar(select(User).where(User.display_name == "Аня"))
    await client.post("/logout")

    await _login(client, "Кирилл", teacher=True)
    response = await client.post(f"/teacher/groups/{algo.id}/remove/{anya.id}")
    assert response.status_code == 200

    left = list(
        (
            await session.execute(
                select(GroupMembership).where(GroupMembership.user_id == anya.id)
            )
        ).scalars()
    )
    assert [m.group_id for m in left] == [structures.id]


async def test_student_cannot_remove_anyone(session, client):
    """Исключение — право преподавателя, и проверяется оно на сервере."""
    await _login(client, "Кирилл", teacher=True)
    await client.post("/teacher/groups", data={"title": "Алгоритмы"})
    group = await session.scalar(select(Group))
    await client.post("/logout")

    await _login(client, "Аня")
    await client.post("/groups/join", data={"join_code": group.join_code})
    anya = await session.scalar(select(User).where(User.display_name == "Аня"))

    response = await client.post(f"/teacher/groups/{group.id}/remove/{anya.id}")
    assert "только для преподавателя" in response.text
    assert await session.scalar(
        select(GroupMembership).where(GroupMembership.user_id == anya.id)
    )


async def test_teacher_adds_a_student_to_a_group(session, client):
    """Код вступления подходит не всем: кто-то приходит в середине семестра."""
    await _login(client, "Кирилл", teacher=True)
    await client.post("/teacher/groups", data={"title": "Алгоритмы"})
    group = await session.scalar(select(Group))
    await client.post("/logout")

    await _login(client, "Аня")          # просто вошла, в группу не вступала
    anya = await session.scalar(select(User).where(User.display_name == "Аня"))
    await client.post("/logout")

    await _login(client, "Кирилл", teacher=True)
    page = await client.get(f"/teacher/groups/{group.id}")
    assert "Добавить студента" in page.text and "Аня" in page.text

    response = await client.post(f"/teacher/groups/{group.id}/members", data={"user_id": anya.id})
    assert "в группе" in response.text
    assert await session.scalar(
        select(GroupMembership).where(
            GroupMembership.group_id == group.id, GroupMembership.user_id == anya.id
        )
    )


async def test_adding_the_same_student_twice_is_harmless(session, client):
    await _login(client, "Кирилл", teacher=True)
    await client.post("/teacher/groups", data={"title": "Алгоритмы"})
    group = await session.scalar(select(Group))
    await client.post("/logout")
    await _login(client, "Аня")
    anya = await session.scalar(select(User).where(User.display_name == "Аня"))
    await client.post("/logout")

    await _login(client, "Кирилл", teacher=True)
    for _ in range(2):
        await client.post(f"/teacher/groups/{group.id}/members", data={"user_id": anya.id})

    rows = list(
        (
            await session.execute(
                select(GroupMembership).where(GroupMembership.user_id == anya.id)
            )
        ).scalars()
    )
    assert len(rows) == 1


async def test_student_cannot_add_anyone_to_a_group(session, client):
    await _login(client, "Кирилл", teacher=True)
    await client.post("/teacher/groups", data={"title": "Алгоритмы"})
    group = await session.scalar(select(Group))
    await client.post("/logout")

    await _login(client, "Аня")
    anya = await session.scalar(select(User).where(User.display_name == "Аня"))
    response = await client.post(f"/teacher/groups/{group.id}/members", data={"user_id": anya.id})
    assert "только для преподавателя" in response.text


async def test_student_leaves_a_group_and_can_come_back(session, client):
    """Выход возвращается тем же кодом, поэтому подтверждения у кнопки нет."""
    await _login(client, "Кирилл", teacher=True)
    await client.post("/teacher/groups", data={"title": "Алгоритмы"})
    group = await session.scalar(select(Group))
    await client.post("/logout")

    await _login(client, "Аня")
    await client.post("/groups/join", data={"join_code": group.join_code})
    anya = await session.scalar(select(User).where(User.display_name == "Аня"))

    page = await client.get("/")
    assert f"/groups/{group.id}/leave" in page.text

    # Сначала страница подтверждения: выход меняет больше, чем кажется.
    warning = await client.get(f"/groups/{group.id}/leave")
    assert "Что изменится" in warning.text
    assert "Задания группы исчезнут" in warning.text
    assert group.join_code in warning.text        # сказано, как вернуться

    left = await client.post(f"/groups/{group.id}/leave")
    assert "ты вышел" in left.text
    assert not await session.scalar(
        select(GroupMembership).where(GroupMembership.user_id == anya.id)
    )

    await client.post("/groups/join", data={"join_code": group.join_code})
    assert await session.scalar(
        select(GroupMembership).where(GroupMembership.user_id == anya.id)
    )


async def test_leaving_a_foreign_group_changes_nothing(session, client):
    await _login(client, "Кирилл", teacher=True)
    await client.post("/teacher/groups", data={"title": "Алгоритмы"})
    group = await session.scalar(select(Group))
    await client.post("/logout")

    await _login(client, "Аня")
    response = await client.post(f"/groups/{group.id}/leave")
    assert "не в этой группе" in response.text


async def test_students_page_has_two_tabs(session, client):
    """Преподаватели терялись в общем списке, а нужны при выдаче роли."""
    await _login(client, "Кирилл", teacher=True)
    await client.post("/logout")
    await _login(client, "Аня")
    await client.post("/logout")
    await _login(client, "Кирилл", teacher=True)

    students = await client.get("/teacher/students")
    assert "Аня" in students.text and "Кирилл" not in students.text.split("<table")[1]

    teachers = await client.get("/teacher/students?role=teacher")
    assert "Кирилл" in teachers.text
    assert "Аня" not in teachers.text.split("<table")[1]


async def test_teacher_pins_a_group_to_the_panel(session, client):
    """Панель должна открываться на том, с чем работаешь сегодня."""
    await _login(client, "Кирилл", teacher=True)
    await client.post("/teacher/groups", data={"title": "Осень"})
    group = await session.scalar(select(Group))

    panel = await client.get("/teacher")
    assert "Мои группы" not in panel.text          # пока ничего не закреплено

    pinned = await client.post(f"/teacher/groups/{group.id}/favorite")
    assert "Группа закреплена" in pinned.text
    panel = await client.get("/teacher")
    assert "Мои группы" in panel.text and "Осень" in panel.text

    await client.post(f"/teacher/groups/{group.id}/favorite")
    panel = await client.get("/teacher")
    assert "Мои группы" not in panel.text


async def test_rail_differs_for_teacher_and_student(session, client):
    """У преподавателя в рейке его работа, а не чужие задания."""
    await _login(client, "Кирилл", teacher=True)
    rail = (await client.get("/teacher")).text
    assert 'href="/teacher/groups"' in rail
    assert 'href="/teacher/assignments"' in rail
    assert 'href="/teacher/reviews"' in rail
    assert 'href="/accounts"' in rail            # личное осталось ссылкой на панели

    await client.post("/logout")
    await _login(client, "Аня")
    rail = (await client.get("/")).text
    assert 'href="/teacher' not in rail
    assert 'href="/accounts"' in rail
    assert 'href="/announcements"' in rail


async def test_pending_reviews_show_up_in_the_rail(session, client, tmp_path, monkeypatch):
    """Счётчик проверки — единственный способ узнать о работе, не заходя в раздел."""
    from app.models import ReviewStatus, SolutionUpload

    await _login(client, "Кирилл", teacher=True)
    assert "nav-count" not in (await client.get("/teacher")).text

    from app.models import Assignment, ProblemSet

    problem = Problem(platform=Platform.leetcode, external_id="1", slug="two-sum",
                      title="Two Sum", url="", difficulty="Easy")
    problem_set = ProblemSet(title="Разминка")
    author = await session.scalar(select(User))
    session.add_all([problem, problem_set])
    await session.commit()
    assignment = Assignment(title="Неделя 1", problem_set_id=problem_set.id,
                            requires_solution=True)
    session.add(assignment)
    await session.commit()
    session.add(SolutionUpload(
        assignment_id=assignment.id, problem_id=problem.id, user_id=author.id,
        code="print(1)", status=ReviewStatus.pending,
    ))
    await session.commit()

    page = await client.get("/teacher")
    assert "nav-count" in page.text


async def test_platform_accounts_live_in_the_profile(session, client):
    """Привязка аккаунтов — часть настройки себя, рядом с именем и аватаркой."""
    await _login(client, "Аня")
    profile = (await client.get("/me")).text
    assert "Аккаунты платформ" in profile
    assert 'action="/accounts/link"' in profile
    assert 'name="back" value="/me"' in profile      # действие вернёт на профиль

    accounts = (await client.get("/accounts")).text
    assert "Способы входа" in accounts                # вход остался на своей странице
    assert 'action="/accounts/link"' in accounts      # и привязка платформ тоже здесь
    assert 'name="back" value="/accounts"' in accounts


async def test_teacher_can_look_through_student_eyes(session, client):
    """Проверить, что видят студенты, не заводя второй аккаунт."""
    await _login(client, "Кирилл", teacher=True)

    page = (await client.get("/teacher")).text
    assert 'href="/teacher/groups"' in page                  # рейка преподавателя

    switched = await client.post("/view/student", data={"next": "/"})
    assert switched.status_code == 200
    page = (await client.get("/")).text
    assert 'href="/teacher/groups"' not in page              # рейка студенческая
    assert 'href="/accounts"' in page

    back = await client.post("/view/teacher", data={"next": "/teacher"})
    assert back.status_code == 200
    assert 'href="/teacher/groups"' in (await client.get("/teacher")).text


async def test_student_view_hides_teacher_buttons(session, client):
    """Иначе просмотр обманывает: рейка студенческая, а кнопки жюри на месте."""
    await _login(client, "Кирилл", teacher=True)
    assert "Опубликовать материал" in (await client.get("/materials")).text

    await client.post("/view/student", data={"next": "/"})
    assert "Опубликовать материал" not in (await client.get("/materials")).text


async def test_switch_returns_only_to_own_paths(session, client):
    """Адрес возврата приходит формой — уводить им на чужой сайт нельзя."""
    await _login(client, "Кирилл", teacher=True)
    response = await client.post(
        "/view/student", data={"next": "//example.com/"}, follow_redirects=False
    )
    assert response.headers["location"] == "/"
