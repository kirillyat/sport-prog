"""Интеграции: языки программирования у судьи и подключённая языковая модель."""

from __future__ import annotations

import io
import zipfile

import pytest
from sqlalchemy import select

from app.models import Submission
from app.services import judge, languages, llm, tasks

CPP = languages.Language(id=54, name="C++ (GCC 9.2.0)")
PY = languages.Language(id=71, name="Python (3.8.1)")
JAVA = languages.Language(id=62, name="Java (OpenJDK 13.0.1)")


async def _login(client, name, teacher=False):
    data = {"name": name}
    if teacher:
        data["teacher"] = "true"
    await client.post("/login/dev", data=data)


@pytest.fixture
async def task(session):
    files = {
        "sum/statement.md": "---\ntitle: Сумма\n---\n\nСложите числа.\n",
        "sum/tests/open/01.in": "1 2\n",
        "sum/tests/open/01.out": "3\n",
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    problems = await tasks.save(session, tasks.parse_archive(buffer.getvalue()), author_id=None)
    return problems[0]


def _form(page) -> str:
    """Форма редактора на странице задачи."""
    return page.text.split("solution-form", 1)[1].split("</form>", 1)[0]


@pytest.fixture
def seen(monkeypatch):
    """Подменённый судья: всё принимает и запоминает, на каком языке звали."""
    calls: list[int | None] = []

    async def fake_run(cfg, code, tests, time_limit_ms, memory_limit_mb, language_id=None):
        calls.append(language_id)
        return judge.RunResult(results=[
            judge.TestResult(position=t.position, is_open=t.is_open, passed=True,
                             status="Accepted", stdout="", stderr="")
            for t in tests
        ])

    monkeypatch.setattr(judge, "run", fake_run)
    return calls


# ---------------------------------------------------------------- языки


async def test_without_a_choice_students_write_python_as_before(session, client, task, seen):
    """Стенд с идущим курсом после обновления не меняется: один язык, без выбора."""
    await judge.connect(session, "http://judge.test")
    await _login(client, "Кирилл", teacher=True)
    await client.post("/logout")
    await _login(client, "Аня")

    form = _form(await client.get(f"/tasks/{task.slug}"))
    assert 'name="language_id"' in form and "<select" not in form

    await client.post(f"/tasks/{task.slug}/submit", data={"code": "print(3)"})
    assert seen == [71]
    sub = await session.scalar(select(Submission))
    assert sub.language == "Python 3"


async def test_student_submits_in_a_language_the_teacher_allowed(session, client, task, seen):
    await judge.connect(session, "http://judge.test")
    await languages.save(session, [CPP, PY])
    await _login(client, "Кирилл", teacher=True)
    await client.post("/logout")
    await _login(client, "Аня")

    page = (await client.get(f"/tasks/{task.slug}")).text
    assert "C++ (GCC 9.2.0)" in page and "data-language-pick" in page

    await client.post(f"/tasks/{task.slug}/submit",
                      data={"code": "int main(){}", "language_id": "54"})
    assert seen == [54]
    sub = await session.scalar(select(Submission))
    assert sub.language == "C++ (GCC 9.2.0)"

    # Открыл задачу снова — выбран язык прошлой сдачи.
    form = _form(await client.get(f"/tasks/{task.slug}"))
    option = form.split('<option value="54"', 1)[1].split(">", 1)[0]
    assert "selected" in option


async def test_a_language_outside_the_list_is_not_sent_to_the_judge(session, client, task, seen):
    """Номер подставили в форму руками — судья получает разрешённый язык,
    а не любой из своих десятков."""
    await judge.connect(session, "http://judge.test")
    await languages.save(session, [CPP])
    await _login(client, "Кирилл", teacher=True)
    await client.post("/logout")
    await _login(client, "Аня")

    await client.post(f"/tasks/{task.slug}/run", data={"code": "x", "language_id": "999"})
    assert seen == [54]


async def test_teacher_allows_only_languages_the_judge_offers(session, client, monkeypatch):
    """Имя языка пишется в посылку — его берём у судьи, а не из формы."""
    async def fake_offered(cfg):
        return [CPP, JAVA, PY]

    monkeypatch.setattr(languages, "offered", fake_offered)
    await judge.connect(session, "http://judge.test")
    await _login(client, "Кирилл", teacher=True)

    loaded = await client.post("/teacher/judge/languages/refresh")
    assert "Java (OpenJDK 13.0.1)" in loaded.text

    await client.post("/teacher/judge/languages", data={"language": ["54", "62", "777"]})
    assert [lang.id for lang in await languages.allowed(session)] == [54, 62]


async def test_the_integrations_page_does_not_call_the_judge(session, client, monkeypatch):
    """Лёгший судья держал бы страницу секундами, а на неё идут его чинить."""
    async def must_not_call(cfg):
        raise AssertionError("страница позвонила судье")

    monkeypatch.setattr(languages, "offered", must_not_call)
    await judge.connect(session, "http://judge.test")
    await _login(client, "Кирилл", teacher=True)
    assert (await client.get("/teacher/integrations")).status_code == 200


async def test_old_judge_address_leads_to_integrations(client):
    await _login(client, "Кирилл", teacher=True)
    page = await client.get("/teacher/judge", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"] == "/teacher/integrations"


@pytest.mark.parametrize(("language", "lexer"), [
    ("C++ (GCC 9.2.0)", "cpp"),
    ("GNU G++17 7.3.0", "cpp"),
    ("Python3", "python"),
    ("PyPy 3-64", "python"),
    ("Java (OpenJDK 13.0.1)", "java"),
    ("JavaScript (Node.js 12.14.0)", "javascript"),
    ("C (GCC 9.2.0)", "c"),
    ("C# (Mono 6.6.0.161)", "csharp"),
    ("Brainfuck", ""),
])
def test_submitted_code_is_highlighted_by_its_language(language, lexer):
    assert languages.lexer_name(language) == lexer


# ---------------------------------------------------------------- языковая модель


async def test_a_typo_in_the_model_name_is_reported(session, client, monkeypatch):
    """Опечатка в имени модели — самая частая причина, почему «не работает»."""
    async def fake_models(cfg):
        return ["deepseek-chat", "deepseek-reasoner"]

    monkeypatch.setattr(llm, "models", fake_models)
    await _login(client, "Кирилл", teacher=True)

    page = await client.post("/teacher/llm", data={
        "url": "https://api.example.org/v1", "model": "deepseek-chatt", "token": "sk-1"})
    assert "deepseek-chatt" in page.text and "нет" in page.text

    page = await client.post("/teacher/llm", data={
        "url": "https://api.example.org/v1", "model": "deepseek-chat"})
    assert "Модель подключена" in page.text


async def test_an_empty_key_field_keeps_the_saved_key(session):
    """Поле ключа не показывает сохранённое — смена модели не должна его стирать."""
    await llm.connect(session, "https://api.example.org/v1", "a", "sk-secret")
    await llm.connect(session, "https://api.example.org/v1", "b")
    cfg = await llm.config(session)
    assert (cfg.model, cfg.token) == ("b", "sk-secret")

    await llm.disconnect(session)
    assert (await llm.config(session)).ready is False


async def test_students_cannot_touch_integrations(client):
    await _login(client, "Кирилл", teacher=True)
    await client.post("/logout")
    await _login(client, "Аня")
    page = await client.post("/teacher/llm", data={"url": "http://x", "model": "m"})
    assert "только для преподавателя" in page.text
