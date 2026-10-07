"""Мелкие служебные команды: python -m app.cli <команда>."""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from app.db import SessionLocal, engine
from app.models import Base, Platform, Role, User
from app.services.catalog import sync_catalog
from app.services.sync import sync_all

USAGE = """Команды:
  gen-secret           сгенерировать SECRET_KEY для .env
  backup ФАЙЛ          консистентная копия базы (безопасно на работающем сервисе)
  init-db              создать таблицы напрямую (для тестов; в проде — alembic upgrade head)
  sync-catalog         скачать каталоги задач Codeforces и LeetCode
  sync-submissions     обновить посылки всех подтверждённых аккаунтов
  make-teacher NAME    выдать роль преподавателя пользователю с таким именем
  stats                короткая сводка по базе
  assignments          задания с номерами, группами и дедлайнами
  extend-deadline СРОК НОМЕР...  [--apply]
                       отодвинуть дедлайн заданий (СРОК — местное время,
                       2026-10-03T23:59). Без --apply только показывает,
                       сколько зачётов прибавится; с --apply сначала делает
                       копию базы рядом с ней
"""


def gen_secret() -> None:
    import secrets

    print(f"SECRET_KEY={secrets.token_urlsafe(48)}")


def backup(destination: str) -> None:
    """Копия через backup API SQLite.

    Простой `cp` файла при включённом WAL может дать битую копию: часть
    транзакций лежит в -wal и в основной файл ещё не перенесена.
    """
    import sqlite3
    from pathlib import Path

    from app.config import settings

    source_path = settings.data_dir.resolve() / "sport.db"
    if not source_path.exists():
        print(f"базы нет: {source_path}")
        return

    target = Path(destination).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    try:
        with sqlite3.connect(target) as destination_db:
            source.backup(destination_db)
    finally:
        source.close()
    print(f"копия готова: {target} ({target.stat().st_size} байт)")


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("таблицы созданы")


async def cmd_sync_catalog() -> None:
    async with SessionLocal() as session:
        print(await sync_catalog(session))


async def cmd_sync_submissions() -> None:
    async with SessionLocal() as session:
        print("новых посылок:", await sync_all(session))


async def make_teacher(name: str) -> None:
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.display_name == name))
        if user is None:
            print(f"пользователь «{name}» не найден")
            return
        user.role = Role.teacher
        await session.commit()
        print(f"{name} теперь преподаватель")


async def stats() -> None:
    from sqlalchemy import func

    from app.models import Problem, Submission

    async with SessionLocal() as session:
        for platform in Platform:
            count = await session.scalar(
                select(func.count()).select_from(Problem).where(Problem.platform == platform)
            )
            print(f"{platform.title}: {count} задач в каталоге")
        print("пользователей:", await session.scalar(select(func.count()).select_from(User)))
        print("посылок:", await session.scalar(select(func.count()).select_from(Submission)))


def _target(assignment) -> str:
    if assignment.group is not None:
        return assignment.group.title
    return "персонально" if assignment.user_id else "всем"


async def list_assignments() -> None:
    from app.models import Assignment
    from app.templating import fmt_dt

    async with SessionLocal() as session:
        stmt = select(Assignment).order_by(Assignment.assigned_at.desc())
        for item in (await session.execute(stmt)).scalars().all():
            deadline = fmt_dt(item.deadline) if item.deadline else "без дедлайна"
            print(f"{item.id:>5}  {item.title}  [{_target(item)}]"
                  f"  выдано {fmt_dt(item.assigned_at)}  · дедлайн {deadline}")


async def _credits(session, assignment) -> int:
    from app.services.progress import compute_progress

    progress = await compute_progress(session, assignment)
    return sum(progress.solved_count(user.id) for user in progress.participants)


async def extend_deadlines(raw_deadline: str, ids: list[int], apply: bool) -> int:
    """Тот же перенос, что кнопкой на странице задания, но сразу для многих.

    Сначала всё проверяется и считается, и только если ни одно задание не
    отказало — пишется одной транзакцией. Половина продлённых заданий хуже,
    чем ни одного: потом не вспомнить, какие уже сдвинуты.
    """
    from datetime import datetime

    from app.models import Assignment
    from app.services.deadlines import extend_deadline
    from app.templating import fmt_dt, parse_local_input

    new_deadline = parse_local_input(raw_deadline)
    if new_deadline is None:
        print(f"не понял срок «{raw_deadline}»: нужно вида 2026-10-03T23:59")
        return 1

    async with SessionLocal() as session:
        failed = False
        for assignment_id in ids:
            assignment = await session.get(Assignment, assignment_id)
            if assignment is None:
                print(f"{assignment_id:>5}  задания нет")
                failed = True
                continue
            before = await _credits(session, assignment)
            old = fmt_dt(assignment.deadline) if assignment.deadline else "без дедлайна"
            error = extend_deadline(assignment, new_deadline)
            if error:
                print(f"{assignment_id:>5}  {assignment.title} [{_target(assignment)}]: {error}")
                failed = True
                continue
            after = await _credits(session, assignment)
            print(f"{assignment_id:>5}  {assignment.title} [{_target(assignment)}]: "
                  f"{old} → {fmt_dt(new_deadline)}, зачётов {before} → {after} (+{after - before})")

        if failed:
            await session.rollback()
            print("ничего не изменено: сначала разберись с ошибками выше")
            return 1
        if not apply:
            await session.rollback()
            print("это пробный прогон — чтобы записать, повтори с --apply")
            return 0

        from app.config import settings

        stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        backup(str(settings.data_dir / f"backup-before-extend-{stamp}.db"))
        await session.commit()
        print("записано")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(USAGE)
        return 1
    command, *rest = args
    match command:
        case "gen-secret":
            gen_secret()
        case "backup":
            if not rest:
                print("укажи путь к файлу: python -m app.cli backup ./sport-backup.db")
                return 1
            backup(rest[0])
        case "init-db":
            asyncio.run(init_db())
        case "sync-catalog":
            asyncio.run(cmd_sync_catalog())
        case "sync-submissions":
            asyncio.run(cmd_sync_submissions())
        case "make-teacher":
            if not rest:
                print("укажи имя пользователя")
                return 1
            asyncio.run(make_teacher(" ".join(rest)))
        case "stats":
            asyncio.run(stats())
        case "assignments":
            asyncio.run(list_assignments())
        case "extend-deadline":
            apply = "--apply" in rest
            rest = [arg for arg in rest if arg != "--apply"]
            if len(rest) < 2 or not all(arg.isdigit() for arg in rest[1:]):
                print("пример: python -m app.cli extend-deadline 2026-10-03T23:59 12 13 [--apply]")
                return 1
            return asyncio.run(extend_deadlines(rest[0], [int(arg) for arg in rest[1:]], apply))
        case _:
            print(USAGE)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
