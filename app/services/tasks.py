"""Свои задачи: набор в архиве, условие и тесты на портале.

Набор готовится файлами и кладётся одним zip — так его удобно держать
в репозитории курса, переиспользовать между годами и смотреть в диффах.
Раскладка архива:

    задача/statement.md         условие, сверху поля в YAML (title, лимиты)
    задача/tests/open/01.in     открытый тест: виден студенту
    задача/tests/open/01.out
    задача/tests/closed/01.in   закрытый: не покидает портал
    задача/tests/closed/01.out

Задача становится обычной `Problem` с площадкой `local`, поэтому задания,
матрица, табло и выгрузка работают с ней без единой правки. Здесь только
разбор архива и запись в базу.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Assignment,
    GroupMembership,
    Platform,
    Problem,
    ProblemSetItem,
    Submission,
    Task,
    TaskTest,
    utcnow,
)

# Столько же, сколько у материалов: архив с тестами больше не бывает.
MAX_BYTES = 20 * 1024 * 1024
MAX_TEST_BYTES = 256 * 1024

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)


class ArchiveError(ValueError):
    """Архив не разобрать: не тот формат или задача собрана неправильно."""


@dataclass(slots=True)
class ParsedTest:
    position: int
    is_open: bool
    stdin: str
    expected: str


@dataclass(slots=True)
class ParsedTask:
    slug: str
    title: str
    statement: str
    time_limit_ms: int = 2000
    memory_limit_mb: int = 256
    tests: list[ParsedTest] = field(default_factory=list)

    @property
    def open_tests(self) -> int:
        return sum(1 for test in self.tests if test.is_open)


def _fields(text: str) -> tuple[dict[str, str], str]:
    """Поля YAML сверху условия. Полноценный парсер ради трёх строк не нужен."""
    match = FRONT_MATTER.match(text)
    if not match:
        return {}, text
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip().strip('"').strip("'")
    return fields, match.group(2)


def _number(fields: dict[str, str], key: str, default: int) -> int:
    try:
        return max(1, int(fields[key]))
    except (KeyError, ValueError):
        return default


def parse_archive(data: bytes) -> list[ParsedTask]:
    """Разбирает zip с набором задач. Пустой набор — ошибка, а не тишина."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ArchiveError("это не zip-архив") from exc

    # Архив мог быть собран вместе с общей папкой — снимаем её, если она одна.
    names = [n for n in archive.namelist() if not n.startswith("__MACOSX/")]
    tops = {PurePosixPath(n).parts[0] for n in names if PurePosixPath(n).parts}
    prefix = f"{tops.pop()}/" if len(tops) == 1 and not names[0].endswith(".md") else ""

    statements = sorted(
        n for n in names if n.startswith(prefix) and n.endswith("/statement.md")
    )
    if not statements:
        raise ArchiveError("в архиве нет ни одной задачи: ожидается <задача>/statement.md")

    tasks = []
    for name in statements:
        folder = name[: -len("statement.md")]
        slug = PurePosixPath(folder.rstrip("/")).name.lower()
        if not SLUG_RE.match(slug):
            raise ArchiveError(
                f"«{slug}» не годится в имя папки задачи: латиница, цифры и дефис"
            )
        tasks.append(_task(archive, folder, slug))
    return tasks


def _read(archive: zipfile.ZipFile, name: str) -> str:
    with archive.open(name) as handle:
        raw = handle.read(MAX_TEST_BYTES + 1)
    if len(raw) > MAX_TEST_BYTES:
        raise ArchiveError(f"{name}: файл больше {MAX_TEST_BYTES // 1024} КБ")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArchiveError(f"{name}: файл не в UTF-8") from exc


def _task(archive: zipfile.ZipFile, folder: str, slug: str) -> ParsedTask:
    fields, body = _fields(_read(archive, folder + "statement.md"))
    task = ParsedTask(
        slug=slug,
        title=fields.get("title") or slug,
        statement=body.strip(),
        time_limit_ms=_number(fields, "time_limit_ms", 2000),
        memory_limit_mb=_number(fields, "memory_limit_mb", 256),
    )

    position = 0
    for kind, is_open in (("open", True), ("closed", False)):
        prefix = f"{folder}tests/{kind}/"
        inputs = sorted(n for n in archive.namelist() if n.startswith(prefix) and n.endswith(".in"))
        for name in inputs:
            answer = name[: -len(".in")] + ".out"
            if answer not in archive.namelist():
                raise ArchiveError(f"{name}: нет файла с ответом {PurePosixPath(answer).name}")
            task.tests.append(
                ParsedTest(
                    position=position,
                    is_open=is_open,
                    stdin=_read(archive, name),
                    expected=_read(archive, answer),
                )
            )
            position += 1

    if not task.tests:
        raise ArchiveError(f"«{slug}»: нет ни одного теста")
    if not task.open_tests:
        raise ArchiveError(f"«{slug}»: нет ни одного открытого теста — студенту не на что смотреть")
    return task


async def save(session: AsyncSession, parsed: list[ParsedTask], author_id: int) -> list[Problem]:
    """Кладёт разобранный набор в базу. Задача с тем же слагом обновляется."""
    saved = []
    for item in parsed:
        problem = await session.scalar(
            select(Problem).where(Problem.platform == Platform.local, Problem.slug == item.slug)
        )
        if problem is None:
            problem = Problem(
                platform=Platform.local,
                external_id=item.slug,
                slug=item.slug,
                title=item.title,
                # Задача живёт на портале, внешнего адреса у неё нет.
                url=f"/tasks/{item.slug}",
            )
            session.add(problem)
            await session.flush()
        else:
            problem.title = item.title
            problem.url = f"/tasks/{item.slug}"

        task = await session.scalar(select(Task).where(Task.problem_id == problem.id))
        if task is None:
            task = Task(problem_id=problem.id, created_by_id=author_id)
            session.add(task)
            await session.flush()
        task.statement = item.statement
        task.time_limit_ms = item.time_limit_ms
        task.memory_limit_mb = item.memory_limit_mb
        task.updated_at = utcnow()

        # Тесты заменяем целиком: набор — это снимок, а не приращение.
        # Удаляем одним запросом и сразу: иначе новые строки вставятся раньше
        # удаления старых и упрутся в уникальность номера теста.
        await session.execute(delete(TaskTest).where(TaskTest.task_id == task.id))
        await session.flush()
        for test in item.tests:
            session.add(
                TaskTest(
                    task_id=task.id,
                    position=test.position,
                    is_open=test.is_open,
                    stdin=test.stdin,
                    expected=test.expected,
                )
            )
        saved.append(problem)

    await session.commit()
    return saved


async def get_by_slug(session: AsyncSession, slug: str) -> tuple[Problem, Task] | None:
    problem = await session.scalar(
        select(Problem).where(Problem.platform == Platform.local, Problem.slug == slug)
    )
    if problem is None:
        return None
    task = await session.scalar(select(Task).where(Task.problem_id == problem.id))
    return (problem, task) if task is not None else None


async def tests_for(session: AsyncSession, task: Task, only_open: bool = False) -> list[TaskTest]:
    stmt = select(TaskTest).where(TaskTest.task_id == task.id)
    if only_open:
        stmt = stmt.where(TaskTest.is_open.is_(True))
    return list((await session.execute(stmt.order_by(TaskTest.position))).scalars().all())


async def attempts_used(session: AsyncSession, user_id: int, problem_id: int) -> int:
    """Сколько раз студент уже сдавал эту задачу."""
    return await session.scalar(
        select(func.count())
        .select_from(Submission)
        .where(
            Submission.user_id == user_id,
            Submission.problem_id == problem_id,
            Submission.platform == Platform.local,
        )
    ) or 0


async def my_attempts(session: AsyncSession, user_id: int, problem_id: int) -> list[Submission]:
    stmt = (
        select(Submission)
        .where(
            Submission.user_id == user_id,
            Submission.problem_id == problem_id,
            Submission.platform == Platform.local,
        )
        .order_by(Submission.submitted_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def assignment_for(session: AsyncSession, user_id: int, problem_id: int) -> Assignment | None:
    """Задание студента, в которое входит эта задача. Нужно, чтобы решение
    попало преподавателю в ту же очередь проверки, что и остальные."""
    groups = select(GroupMembership.group_id).where(GroupMembership.user_id == user_id)
    stmt = (
        select(Assignment)
        .join(ProblemSetItem, ProblemSetItem.problem_set_id == Assignment.problem_set_id)
        .where(
            ProblemSetItem.problem_id == problem_id,
            (Assignment.user_id == user_id)
            | (Assignment.group_id.in_(groups))
            | ((Assignment.group_id.is_(None)) & (Assignment.user_id.is_(None))),
        )
        .order_by(Assignment.assigned_at.desc())
    )
    return await session.scalar(stmt)
