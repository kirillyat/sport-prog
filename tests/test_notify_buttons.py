"""Кнопка под сообщением в Telegram и запасной путь, если её не принимают."""

from __future__ import annotations

import json

import pytest

from app import notify
from app.config import settings
from app.models import Announcement, Assignment, Material

BUTTON = ("Открыть задание", "https://sport.ai.msu.ru/assignments/7")


class FakeAPI:
    """Считает попытки и умеет отказать на первой — как Telegram на плохой кнопке."""

    def __init__(self, fail_first: bool = False) -> None:
        self.calls: list[dict] = []
        self.fail_first = fail_first

    async def call(self, method, **payload):
        self.calls.append({"method": method, **payload})
        if self.fail_first and len(self.calls) == 1:
            return None
        return {"message_id": len(self.calls)}

    async def upload(self, method, files, **payload):
        return await self.call(method, **payload)

    async def close(self):
        return None


@pytest.fixture
def api(monkeypatch):
    created = []

    def factory(fail_first=False):
        fake = FakeAPI(fail_first)
        created.append(fake)
        monkeypatch.setattr(notify, "TelegramAPI", lambda token: fake)
        monkeypatch.setattr(settings, "telegram_bot_token", "token")
        return fake

    return factory


def test_button_needs_a_public_address():
    assert notify.button_markup(*BUTTON) is not None
    # Локальный адрес Telegram не примет, да и нажать его с телефона нельзя.
    assert notify.button_markup("Открыть", "http://localhost:8000/materials") is None
    assert notify.button_markup("Открыть", "http://127.0.0.1:8000/materials") is None
    assert notify.button_markup("Открыть", "ftp://example.com/file") is None


async def test_message_carries_the_button(api):
    fake = api()
    assert await notify.send_many([42], "Текст", BUTTON) == 1

    payload = fake.calls[0]
    assert payload["method"] == "sendMessage"
    assert payload["text"] == "Текст"                 # ссылку в текст не дублируем
    keyboard = json.loads(payload["reply_markup"])["inline_keyboard"]
    assert keyboard == [[{"text": BUTTON[0], "url": BUTTON[1]}]]


async def test_local_address_falls_back_to_a_link_in_the_text(api):
    fake = api()
    local = ("Открыть на портале", "http://localhost:8000/materials")
    assert await notify.send_many([42], "Текст", local) == 1

    payload = fake.calls[0]
    assert "reply_markup" not in payload
    assert 'href="http://localhost:8000/materials"' in payload["text"]


async def test_refused_button_is_retried_without_it(api):
    fake = api(fail_first=True)
    assert await notify.send_many([42], "Текст", BUTTON) == 1

    assert len(fake.calls) == 2
    assert "reply_markup" in fake.calls[0]
    assert "reply_markup" not in fake.calls[1]
    assert 'href="https://sport.ai.msu.ru/assignments/7"' in fake.calls[1]["text"]


async def test_document_gets_the_button_too(api):
    fake = api()
    sent = await notify.send_document_many([42], "seminar.ipynb", b"{}", "Подпись", BUTTON)

    assert sent == 1
    payload = fake.calls[0]
    assert payload["method"] == "sendDocument"
    assert payload["caption"] == "Подпись"
    assert json.loads(payload["reply_markup"])["inline_keyboard"][0][0]["url"] == BUTTON[1]


def test_buttons_point_where_expected(monkeypatch):
    monkeypatch.setattr(settings, "base_url", "https://sport.ai.msu.ru/")

    assignment = Assignment(id=7, title="Неделя 3", problem_set_id=1)
    material = Material(id=4, title="Семинар", stored_name="a", filename="a.ipynb",
                        content_type="text/plain", size=1)
    assert notify.assignment_button(assignment)[1] == "https://sport.ai.msu.ru/assignments/7"
    assert notify.material_button(material)[1] == "https://sport.ai.msu.ru/materials/4/view"

    # У анонса своя ссылка — она важнее портала.
    contest = Announcement(title="Раунд", url="https://codeforces.com/contest/1",
                           url_label="Регистрация")
    assert notify.announcement_button(contest) == ("Регистрация", "https://codeforces.com/contest/1")
    plain = Announcement(title="Сбор")
    assert plain.url is None
    assert notify.announcement_button(plain)[1] == "https://sport.ai.msu.ru/announcements"
