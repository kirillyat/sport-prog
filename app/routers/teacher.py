from __future__ import annotations

import secrets
import string
from datetime import datetime, timedelta
from typing import Annotated
from urllib.parse import quote, urlparse

from fastapi import APIRouter, BackgroundTasks, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.deps import AdminUser, OptionalInt, SessionDep, TeacherUser
from app.i18n import translate as _
from app.models import (
    Announcement,
    Assignment,
    BonusPoint,
    Group,
    GroupFavorite,
    GroupMembership,
    Platform,
    Problem,
    ProblemSet,
    ProblemSetItem,
    ReviewStatus,
    Role,
    SolutionUpload,
    Submission,
    User,
    utcnow,
)
from app.routers.leaderboard import PERIODS
from app.services import (
    announcements,
    assignments,
    export,
    features,
    judge,
    languages,
    llm,
    messages,
    notebook,
    problem_sets,
    similarity,
    solutions,
    timeline,
)
from app.services import tasks as tasks_service
from app.services.catalog import get_state, problem_count, set_state, sync_catalog
from app.services.deadlines import extend_deadline
from app.services.feed import build_feed
from app.services.leaderboard import build_leaderboard
from app.services.problem_parser import search_problems
from app.services.progress import compute_progress, participants_for_assignment
from app.services.sync import (
    relink_orphan_submissions,
    sync_all,
    sync_users_in_background,
)
from app.templating import LOCAL_TZ, parse_local_input, templates

router = APIRouter(prefix="/teacher", tags=["teacher"])

JOIN_ALPHABET = string.ascii_uppercase + string.digits


def _redirect(path: str, message: str | None = None, error: str | None = None):
    params = []
    if message:
        params.append("ok=" + quote(message))
    if error:
        params.append("err=" + quote(error))
    suffix = ("?" + "&".join(params)) if params else ""
    return RedirectResponse(f"{path}{suffix}", status_code=303)


async def _all(session: AsyncSession, stmt) -> list:
    return list((await session.execute(stmt)).scalars().all())


async def _new_join_code(session: AsyncSession) -> str:
    while True:
        code = "".join(secrets.choice(JOIN_ALPHABET) for _ in range(6))
        exists = await session.scalar(select(Group.id).where(Group.join_code == code))
        if not exists:
            return code


def _flash(request: Request) -> dict[str, str | None]:
    return {"ok": request.query_params.get("ok"), "error": request.query_params.get("err")}


# ---------------------------------------------------------------- обзор


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


@router.get("")
async def overview(request: Request, session: SessionDep, user: TeacherUser):
    counts = {
        "students": await session.scalar(
            select(func.count()).select_from(User).where(User.role == Role.student)
        ),
        "groups": await session.scalar(
            select(func.count()).select_from(Group).where(Group.is_archived.is_(False))
        ),
        "sets": await session.scalar(select(func.count()).select_from(ProblemSet)),
        "assignments": await session.scalar(select(func.count()).select_from(Assignment)),
        "submissions": await session.scalar(select(func.count()).select_from(Submission)),
    }
    catalog = {p.title: await problem_count(session, p) for p in Platform.external()}

    # Закреплённые группы — первыми, остальные следом: панель должна открываться
    # на том, с чем работаешь сегодня, а не на полном списке потоков.
    pinned_ids = set(
        await _all(
            session, select(GroupFavorite.group_id).where(GroupFavorite.user_id == user.id)
        )
    )
    groups = await _all(
        session, select(Group).where(Group.is_archived.is_(False)).order_by(Group.title)
    )
    sizes = dict(
        (
            await session.execute(
                select(GroupMembership.group_id, func.count())
                .group_by(GroupMembership.group_id)
            )
        ).all()
    )
    pinned = [
        {"group": g, "size": sizes.get(g.id, 0)} for g in groups if g.id in pinned_ids
    ]

    # Что идёт сейчас: выданное и ещё не просроченное, вместе с долей закрытого.
    now = utcnow()
    running = []
    stmt = (
        select(Assignment)
        .where(
            Assignment.assigned_at <= now,
            (Assignment.deadline.is_(None)) | (Assignment.deadline >= now),
        )
        .order_by(Assignment.assigned_at.desc())
        .limit(6)
    )
    for assignment in await _all(session, stmt):
        participants = await participants_for_assignment(session, assignment)
        progress = await compute_progress(session, assignment, participants)
        done = sum(1 for p in participants if progress.is_complete(p.id))
        solved = sum(progress.solved_count(p.id) for p in participants)
        total = progress.total_problems * len(participants)
        running.append(
            {
                "assignment": assignment,
                "people": len(participants),
                "done": done,
                "share": round(100 * solved / total) if total else 0,
            }
        )
    # Пока курс не собран целиком, панель показывает шаги, а не нули в плитках.
    steps = [
        {"done": bool(counts["groups"]), "title": _("Создать группу"),
         "hint": _("И раздать студентам код вступления"), "href": "/teacher/groups"},
        {"done": bool(counts["sets"]), "title": _("Собрать список задач"),
         "hint": _("Вставить ссылки на задачи текстом"), "href": "/teacher/sets"},
        {"done": bool(counts["assignments"]), "title": _("Выдать задание"),
         "hint": _("Назначить список группе с дедлайном"), "href": "/teacher/assignments"},
    ]
    return templates.TemplateResponse(
        request,
        "teacher/overview.html",
        {
            "user": user,
            "counts": counts,
            "pinned": pinned,
            "running": running,
            "steps": steps,
            "onboarding": not all(step["done"] for step in steps),
            "catalog": catalog,
            "catalog_synced_at": _parse_iso(await get_state(session, "catalog_synced_at")),
            **_flash(request),
        },
    )


@router.get("/tasks")
async def own_tasks(request: Request, session: SessionDep, user: TeacherUser):
    """Свои задачи: список и загрузка набора архивом."""
    from app.models import Platform, Task

    rows = await _all(
        session,
        select(Problem).where(Problem.platform == Platform.local).order_by(Problem.title),
    )
    counts = {}
    for problem in rows:
        task = await session.scalar(select(Task).where(Task.problem_id == problem.id))
        if task is None:
            continue
        tests = await tasks_service.tests_for(session, task)
        counts[problem.id] = (len(tests), sum(1 for t in tests if t.is_open))
    return templates.TemplateResponse(
        request,
        "teacher/tasks.html",
        {
            "user": user,
            "problems": rows,
            "counts": counts,
            "judge": await judge.config(session),
            "health": await judge.health(session),
            **_flash(request),
        },
    )


@router.post("/tasks")
async def upload_tasks(
    session: SessionDep, user: TeacherUser, file: UploadFile = File(...)
):
    """Загрузка набора задач архивом. Задача с тем же именем папки обновляется."""
    if file.size and file.size > tasks_service.MAX_BYTES:
        return _redirect(
            "/teacher/tasks",
            error=_("Архив больше %(mb)s МБ") % {"mb": tasks_service.MAX_BYTES // 1024 // 1024},
        )
    data = await file.read()
    try:
        parsed = tasks_service.parse_archive(data)
    except tasks_service.ArchiveError as exc:
        return _redirect("/teacher/tasks", error=_("Архив не разобрать: %(why)s") % {"why": exc})

    saved = await tasks_service.save(session, parsed, user.id)
    return _redirect(
        "/teacher/tasks", message=_("Загружено задач: %(count)s") % {"count": len(saved)}
    )


INTEGRATIONS = "/teacher/integrations"


@router.get("/integrations")
async def integrations_page(request: Request, session: SessionDep, user: AdminUser):
    """Внешние сервисы портала: судья, языки программирования, языковая модель.

    Отдельный раздел, а не часть страницы задач: задачи заводят заранее и
    один раз, судью подключают в день контрольной и гасят после неё.
    """
    cfg = await judge.config(session)
    health = await judge.health(session)
    return templates.TemplateResponse(
        request,
        "teacher/integrations.html",
        {
            "user": user,
            "judge": cfg,
            "health": health,
            "from_env": bool(settings.judge0_url.strip()),
            "allowed": await languages.allowed(session),
            "offered": await languages.cached_offered(session),
            "default_language": languages.default(),
            "llm": await llm.config(session),
            "llm_health": await llm.health(session),
            **_flash(request),
        },
    )


@router.get("/judge")
async def judge_page_moved(user: AdminUser):
    """Старый адрес раздела: он в закладках и в инструкциях к контрольной."""
    return _redirect(INTEGRATIONS)


@router.post("/judge")
async def judge_connect(
    session: SessionDep, user: AdminUser, url: str = Form(""), token: str = Form("")
):
    """Подключить тестирующую систему. Адрес пишем в базу — судью поднимают
    перед контрольной и гасят после, портал ради этого не перезапускают.

    Адрес задаёт преподаватель, и портал будет ходить по нему сам: это доверие
    к роли, а не к вводу, поэтому форма открыта только преподавателю.
    """
    if not url.strip():
        return _redirect(INTEGRATIONS, error=_("Адрес пустой"))

    await judge.connect(session, url, token)
    health = (await judge.check(session))[1]
    if not health.ok:
        # Адрес всё равно сохранён: судья может ещё подниматься, а вводить
        # его заново, когда он ответит, — лишняя работа.
        return _redirect(
            INTEGRATIONS,
            error=_("Адрес сохранён, но судья не отвечает: %(why)s") % {"why": health.detail},
        )
    return _redirect(
        INTEGRATIONS,
        message=_("Судья подключён, версия %(version)s") % {"version": health.version},
    )


@router.post("/judge/check")
async def judge_check(session: SessionDep, user: AdminUser):
    config, health = await judge.check(session)
    if not config.ready:
        return _redirect(INTEGRATIONS, error=_("Судья не подключён"))
    if not health.ok:
        return _redirect(
            INTEGRATIONS, error=_("Судья не отвечает: %(why)s") % {"why": health.detail}
        )
    return _redirect(
        INTEGRATIONS,
        message=_("Судья отвечает, версия %(version)s") % {"version": health.version},
    )


@router.post("/judge/off")
async def judge_off(session: SessionDep, user: AdminUser):
    """Отключить судью: решения снова принимаются как код, без вердикта."""
    await judge.disconnect(session)
    return _redirect(INTEGRATIONS, message=_("Судья отключён"))


@router.post("/judge/languages")
async def judge_languages(request: Request, session: SessionDep, user: AdminUser):
    """Какие языки судьи разрешены студентам.

    Имена берём у судьи, а не из формы: в посылку пишется имя языка, и
    подсунуть туда что угодно через поддельную форму не должно получиться.
    """
    form = await request.form()
    wanted = set()
    for raw in form.getlist("language"):
        try:
            wanted.add(int(raw))
        except ValueError:
            continue
    offered = await languages.cached_offered(session)
    chosen = [lang for lang in offered if lang.id in wanted]
    await languages.save(session, chosen)
    if not chosen:
        return _redirect(
            INTEGRATIONS,
            message=_("Языки не выбраны — студенты пишут на %(name)s")
            % {"name": languages.default().name},
        )
    return _redirect(
        INTEGRATIONS, message=_("Разрешено языков: %(count)s") % {"count": len(chosen)}
    )


@router.post("/judge/languages/refresh")
async def judge_languages_refresh(session: SessionDep, user: AdminUser):
    try:
        offered = await languages.refresh_offered(session, await judge.config(session))
    except judge.JudgeUnavailable as exc:
        return _redirect(INTEGRATIONS, error=_("Судья не отвечает: %(why)s") % {"why": exc})
    return _redirect(
        INTEGRATIONS, message=_("Языков у судьи: %(count)s") % {"count": len(offered)}
    )


@router.post("/llm")
async def llm_connect(
    session: SessionDep,
    user: AdminUser,
    url: str = Form(""),
    model: str = Form(""),
    token: str = Form(""),
):
    if not url.strip() or not model.strip():
        return _redirect(INTEGRATIONS, error=_("Нужны адрес и модель"))
    await llm.connect(session, url, model, token)
    health = (await llm.check(session))[1]
    if not health.ok:
        return _redirect(
            INTEGRATIONS,
            error=_("Сохранено, но модель не отвечает: %(why)s") % {"why": health.detail},
        )
    return _redirect(INTEGRATIONS, message=_("Модель подключена"))


@router.post("/llm/check")
async def llm_check(session: SessionDep, user: AdminUser):
    config, health = await llm.check(session)
    if not config.url:
        return _redirect(INTEGRATIONS, error=_("Модель не подключена"))
    if not health.ok:
        return _redirect(
            INTEGRATIONS, error=_("Модель не отвечает: %(why)s") % {"why": health.detail}
        )
    return _redirect(INTEGRATIONS, message=_("Модель отвечает"))


@router.post("/llm/off")
async def llm_off(session: SessionDep, user: AdminUser):
    await llm.disconnect(session)
    return _redirect(INTEGRATIONS, message=_("Модель отключена"))


@router.get("/judge/check")
@router.get("/judge/off")
@router.get("/judge/languages")
@router.get("/judge/languages/refresh")
@router.get("/llm")
@router.get("/llm/check")
@router.get("/llm/off")
async def integration_action_reloaded(user: AdminUser):
    """Пока POST ждёт ответа, в адресной строке уже стоит адрес действия, и
    обновление страницы приходит сюда GET-ом. Вместо голого 405 возвращаем
    в раздел: действие повторять не нужно, состояние и так видно там."""
    return _redirect(INTEGRATIONS)


@router.post("/course-refresh")
async def course_refresh(session: SessionDep, user: AdminUser):
    """Забрать материалы курса прямо сейчас, не дожидаясь планировщика."""
    from app.scheduler import COURSE_KEY
    from app.services import course

    if not settings.course_source_url:
        return _redirect("/teacher", error=_("Источник материалов не настроен"))
    try:
        files = await course.refresh()
    except course.CourseSourceError as exc:
        return _redirect("/teacher", error=_("Материалы не обновились: %(why)s") % {"why": exc})
    await set_state(session, COURSE_KEY, utcnow().isoformat())
    return _redirect(
        "/teacher", message=_("Материалы обновлены, файлов: %(count)s") % {"count": files}
    )


@router.post("/sync-now")
async def sync_now(session: SessionDep, user: TeacherUser):
    added = await sync_all(session)
    return _redirect(
        "/teacher",
        message=_("Синхронизация завершена, новых посылок: %(count)s") % {"count": added},
    )


@router.post("/catalog-refresh")
async def catalog_refresh(session: SessionDep, user: AdminUser):
    counts = await sync_catalog(session)
    fixed = await relink_orphan_submissions(session)
    total = sum(counts.values())
    return _redirect(
        "/teacher",
        message=_("Каталог обновлён: %(total)s задач, подвязано посылок: %(fixed)s")
        % {"total": total, "fixed": fixed},
    )


# ---------------------------------------------------------------- группы


@router.get("/groups")
async def groups_page(request: Request, session: SessionDep, user: TeacherUser):
    groups = await _all(session, select(Group).order_by(Group.is_archived, Group.title))
    sizes = dict(
        (
            await session.execute(
                select(GroupMembership.group_id, func.count()).group_by(GroupMembership.group_id)
            )
        ).all()
    )
    return templates.TemplateResponse(
        request,
        "teacher/groups.html",
        {"user": user, "groups": groups, "sizes": sizes, **_flash(request)},
    )


@router.post("/groups")
async def create_group(session: SessionDep, user: TeacherUser, title: str = Form(...)):
    title = title.strip()
    if not title:
        return _redirect("/teacher/groups", error=_("Пустое название"))
    group = Group(title=title, join_code=await _new_join_code(session), created_by_id=user.id)
    session.add(group)
    await session.commit()
    return _redirect("/teacher/groups", message=_("Группа «%(title)s» создана") % {"title": title})


@router.get("/groups/{group_id}")
async def group_detail(request: Request, session: SessionDep, user: TeacherUser, group_id: int):
    group = await session.get(Group, group_id)
    if group is None:
        return _redirect("/teacher/groups", error=_("Группа не найдена"))
    members = await _all(
        session,
        select(User)
        .join(GroupMembership, GroupMembership.user_id == User.id)
        .where(GroupMembership.group_id == group_id)
        .order_by(User.display_name),
    )
    assignments = await _all(
        session,
        select(Assignment)
        .where(Assignment.group_id == group_id)
        .order_by(Assignment.assigned_at.desc()),
    )
    # Кого можно добавить: активные студенты, которых в группе ещё нет.
    # Преподавателей не предлагаем — в матрице им делать нечего.
    in_group = select(GroupMembership.user_id).where(GroupMembership.group_id == group_id)
    candidates = await _all(
        session,
        select(User)
        .where(
            User.is_active.is_(True),
            User.role != Role.teacher,
            User.id.not_in(in_group),
        )
        .order_by(User.display_name),
    )
    return templates.TemplateResponse(
        request,
        "teacher/group.html",
        {
            "user": user,
            "group": group,
            "members": members,
            "candidates": candidates,
            "is_favorite": await session.scalar(
                select(GroupFavorite.id).where(
                    GroupFavorite.user_id == user.id, GroupFavorite.group_id == group_id
                )
            )
            is not None,
            "assignments": assignments,
            "feed_items": await build_feed(session, user, group_id=group_id, limit=20),
            **_flash(request),
        },
    )


@router.post("/groups/{group_id}/chat")
async def set_group_chat(
    session: SessionDep, user: TeacherUser, group_id: int, chat_id: str = Form("")
):
    group = await session.get(Group, group_id)
    if group is None:
        return _redirect("/teacher/groups", error=_("Группа не найдена"))
    chat = chat_id.strip()
    if chat and not chat.lstrip("-").isdigit():
        return _redirect(
            f"/teacher/groups/{group_id}", error=_("Id чата — число, например -1001234567890")
        )
    group.telegram_chat_id = chat or None
    await session.commit()
    message = _("Чат группы сохранён") if chat else _("Чат группы отвязан")
    return _redirect(f"/teacher/groups/{group_id}", message=message)


@router.post("/groups/{group_id}/favorite")
async def toggle_favorite(session: SessionDep, user: TeacherUser, group_id: int):
    """Закрепить группу у себя на панели или снять закрепление."""
    group = await session.get(Group, group_id)
    if group is None:
        return _redirect("/teacher/groups", error=_("Группа не найдена"))

    existing = await session.scalar(
        select(GroupFavorite).where(
            GroupFavorite.user_id == user.id, GroupFavorite.group_id == group_id
        )
    )
    if existing is None:
        session.add(GroupFavorite(user_id=user.id, group_id=group_id))
        message = _("Группа закреплена")
    else:
        await session.delete(existing)
        message = _("Группа откреплена")
    await session.commit()
    return _redirect(f"/teacher/groups/{group_id}", message=message)


@router.post("/groups/{group_id}/sync")
async def sync_group(
    session: SessionDep, user: TeacherUser, group_id: int, background: BackgroundTasks
):
    """Обновить результаты всей группы разом — вместо обхода профилей по одному."""
    group = await session.get(Group, group_id)
    if group is None:
        return _redirect("/teacher/groups", error=_("Группа не найдена"))

    ids = await _all(
        session,
        select(GroupMembership.user_id).where(GroupMembership.group_id == group_id),
    )
    if not ids:
        return _redirect(f"/teacher/groups/{group_id}", error=_("В группе никого нет"))

    background.add_task(sync_users_in_background, list(ids))
    return _redirect(
        f"/teacher/groups/{group_id}",
        message=_("Обновляю результаты: %(count)s студентов. Займёт около минуты")
        % {"count": len(ids)},
    )


@router.post("/assignments/{assignment_id}/sync")
async def sync_assignment(
    session: SessionDep, user: TeacherUser, assignment_id: int, background: BackgroundTasks
):
    """То же самое со страницы задания: обновить всех, кто его получил."""
    assignment = await session.get(Assignment, assignment_id)
    if assignment is None:
        return _redirect("/teacher/assignments", error=_("Задание не найдено"))

    participants = await participants_for_assignment(session, assignment)
    if not participants:
        return _redirect(f"/teacher/assignments/{assignment_id}", error=_("Участников нет"))

    background.add_task(sync_users_in_background, [p.id for p in participants])
    return _redirect(
        f"/teacher/assignments/{assignment_id}",
        message=_("Обновляю результаты: %(count)s студентов. Займёт около минуты")
        % {"count": len(participants)},
    )


@router.post("/groups/{group_id}/members")
async def add_member(
    session: SessionDep, user: TeacherUser, group_id: int, user_id: int = Form(...)
):
    """Добавить студента в группу руками: код вступления подходит не всем.

    Кто-то приходит в середине семестра, кого-то перевели из другой группы,
    а кто-то просто не дошёл до кода. Списывать это на самообслуживание —
    значит оставлять преподавателя без инструмента.
    """
    group = await session.get(Group, group_id)
    if group is None:
        return _redirect("/teacher/groups", error=_("Группа не найдена"))

    student = await session.get(User, user_id)
    if student is None or not student.is_active:
        return _redirect(f"/teacher/groups/{group_id}", error=_("Студент не найден"))
    if student.is_teacher:
        return _redirect(
            f"/teacher/groups/{group_id}", error=_("Преподавателя в группу не добавляем")
        )

    existing = await session.scalar(
        select(GroupMembership).where(
            GroupMembership.group_id == group_id, GroupMembership.user_id == user_id
        )
    )
    if existing is None:
        session.add(GroupMembership(group_id=group_id, user_id=user_id))
        await session.commit()
    name = quote(student.display_name)
    return _redirect(f"/teacher/groups/{group_id}", message=_("%(name)s в группе") % {"name": name})


@router.post("/groups/{group_id}/remove/{user_id}")
async def remove_member(session: SessionDep, user: TeacherUser, group_id: int, user_id: int):
    membership = await session.scalar(
        select(GroupMembership).where(
            GroupMembership.group_id == group_id, GroupMembership.user_id == user_id
        )
    )
    if membership is not None:
        await session.delete(membership)
        await session.commit()
    return _redirect(f"/teacher/groups/{group_id}", message=_("Студент исключён"))


async def _review_rows(session: SessionDep, uploads: list) -> list[dict]:
    """Подтягиваем к решениям автора, задачу и задание одним проходом."""
    rows = []
    for upload in uploads:
        rows.append(
            {
                "upload": upload,
                "author": await session.get(User, upload.user_id),
                "problem": await session.get(Problem, upload.problem_id),
                "assignment": await session.get(Assignment, upload.assignment_id),
            }
        )
    return rows


@router.get("/reviews")
async def reviews_page(request: Request, session: SessionDep, user: TeacherUser):
    waiting = await solutions.pending(session)
    done = list(
        (
            await session.execute(
                select(SolutionUpload)
                .where(SolutionUpload.status != ReviewStatus.pending)
                .order_by(SolutionUpload.reviewed_at.desc())
                .limit(20)
            )
        ).scalars()
    )
    return templates.TemplateResponse(
        request,
        "teacher/reviews.html",
        {
            "user": user,
            "rows": await _review_rows(session, waiting),
            "reviewed": await _review_rows(session, done),
            "ok": request.query_params.get("ok"),
            "error": request.query_params.get("err"),
        },
    )


@router.get("/features")
async def features_page(request: Request, session: SessionDep, user: AdminUser):
    flags = await features.load(session)
    rows = [
        {
            "section": section,
            "for_students": flags.get(section.key, features.DEFAULT)[0],
            "for_teachers": flags.get(section.key, features.DEFAULT)[1],
        }
        for section in features.SECTIONS
    ]
    return templates.TemplateResponse(
        request,
        "teacher/features.html",
        {
            "user": user,
            "rows": rows,
            "ok": request.query_params.get("ok"),
            "error": request.query_params.get("err"),
        },
    )


@router.post("/features")
async def save_features(request: Request, session: SessionDep, user: AdminUser):
    """Галочки приходят только для включённых — остальное считаем выключенным."""
    form = await request.form()
    for section in features.SECTIONS:
        await features.save(
            session,
            section.key,
            for_students=f"{section.key}:students" in form,
            for_teachers=f"{section.key}:teachers" in form,
        )
    return _redirect("/teacher/features", message=_("Видимость разделов сохранена"))


@router.post("/groups/{group_id}/archive")
async def archive_group(session: SessionDep, user: TeacherUser, group_id: int):
    group = await session.get(Group, group_id)
    if group is None:
        return _redirect("/teacher/groups", error=_("Группа не найдена"))
    group.is_archived = not group.is_archived
    await session.commit()
    return _redirect("/teacher/groups", message=_("Готово"))


# ---------------------------------------------------------------- списки задач


@router.get("/sets")
async def sets_page(request: Request, session: SessionDep, user: TeacherUser):
    sets = await _all(session, select(ProblemSet).order_by(ProblemSet.created_at.desc()))
    return templates.TemplateResponse(
        request, "teacher/sets.html", {"user": user, "sets": sets, **_flash(request)}
    )


@router.post("/sets")
async def create_set(
    session: SessionDep,
    user: TeacherUser,
    title: str = Form(...),
    description: str = Form(""),
    problems_text: str = Form(""),
):
    if not title.strip():
        return _redirect("/teacher/sets", error=_("Пустое название"))

    problem_set, result = await problem_sets.create(
        session, user, title, description, problems_text
    )
    if result.unresolved:
        return _redirect(
            f"/teacher/sets/{problem_set.id}",
            error=_("Не распознано: %(list)s") % {"list": ", ".join(result.unresolved[:5])},
        )
    return _redirect(f"/teacher/sets/{problem_set.id}", message=_("Список создан"))


@router.get("/sets/{set_id}")
async def set_detail(request: Request, session: SessionDep, user: TeacherUser, set_id: int):
    problem_set = await session.get(ProblemSet, set_id)
    if problem_set is None:
        return _redirect("/teacher/sets", error=_("Список не найден"))
    used_in = await _all(session, select(Assignment).where(Assignment.problem_set_id == set_id))
    return templates.TemplateResponse(
        request,
        "teacher/set.html",
        {"user": user, "set": problem_set, "used_in": used_in, **_flash(request)},
    )


@router.post("/sets/{set_id}/add")
async def add_problems(
    session: SessionDep, user: TeacherUser, set_id: int, problems_text: str = Form(...)
):
    problem_set = await session.get(ProblemSet, set_id)
    if problem_set is None:
        return _redirect("/teacher/sets", error=_("Список не найден"))

    added_problems, unresolved = await problem_sets.add(session, problem_set, problems_text)
    added = len(added_problems)
    if unresolved:
        return _redirect(
            f"/teacher/sets/{set_id}",
            error=_("Добавлено %(count)s. Не распознано: %(list)s")
            % {"count": added, "list": ", ".join(unresolved[:5])},
        )
    return _redirect(
        f"/teacher/sets/{set_id}", message=_("Добавлено задач: %(count)s") % {"count": added}
    )


@router.post("/sets/{set_id}/remove/{item_id}")
async def remove_problem(session: SessionDep, user: TeacherUser, set_id: int, item_id: int):
    item = await session.get(ProblemSetItem, item_id)
    if item is not None and item.problem_set_id == set_id:
        await problem_sets.remove(session, set_id, [item.problem_id])
    return _redirect(f"/teacher/sets/{set_id}", message=_("Задача убрана"))


@router.post("/sets/{set_id}/delete")
async def delete_set(session: SessionDep, user: TeacherUser, set_id: int):
    problem_set = await session.get(ProblemSet, set_id)
    if problem_set is None:
        return _redirect("/teacher/sets", error=_("Список не найден"))
    in_use = await session.scalar(
        select(func.count()).select_from(Assignment).where(Assignment.problem_set_id == set_id)
    )
    if in_use:
        return _redirect("/teacher/sets", error=_("Список используется в заданиях"))
    await session.delete(problem_set)
    await session.commit()
    return _redirect("/teacher/sets", message=_("Список удалён"))


@router.get("/problems")
async def problems_search(
    request: Request,
    session: SessionDep,
    user: TeacherUser,
    q: str = "",
    platform: str | None = None,
):
    target = None
    if platform:
        try:
            target = Platform(platform)
        except ValueError:
            target = None
    results = await search_problems(session, q, target) if (q or target) else []
    return templates.TemplateResponse(
        request,
        "teacher/problems.html",
        {
            "user": user,
            "q": q,
            "platform": platform,
            "platforms": Platform.external(),
            "results": results,
        },
    )


# ---------------------------------------------------------------- задания


@router.get("/assignments")
async def assignments_page(request: Request, session: SessionDep, user: TeacherUser):
    assignments = await _all(session, select(Assignment).order_by(Assignment.assigned_at.desc()))
    groups = await _all(
        session, select(Group).where(Group.is_archived.is_(False)).order_by(Group.title)
    )
    sets = await _all(session, select(ProblemSet).order_by(ProblemSet.title))
    return templates.TemplateResponse(
        request,
        "teacher/assignments.html",
        {
            "user": user,
            "assignments": assignments,
            "groups": groups,
            "sets": sets,
            **_flash(request),
        },
    )


@router.post("/assignments")
async def create_assignment(
    session: SessionDep,
    user: TeacherUser,
    title: str = Form(...),
    problem_set_id: int = Form(...),
    group_id: Annotated[list[str] | None, Form()] = None,
    description: str = Form(""),
    starts_at: str = Form(""),
    deadline: str = Form(""),
    count_prior_solves: bool = Form(False),
    requires_solution: bool = Form(False),
):
    """Все правила задаются здесь: потом меняются название, описание и срок вперёд."""
    try:
        group_ids = [int(raw) for raw in group_id or [] if raw.strip()]
    except ValueError:
        return _redirect("/teacher/assignments", error=_("Группа не найдена"))
    try:
        created, delivered = await assignments.issue(
            session,
            user,
            title=title,
            problem_set_id=problem_set_id,
            group_ids=group_ids,
            description=description,
            starts_at=parse_local_input(starts_at),
            deadline=parse_local_input(deadline),
            count_prior_solves=count_prior_solves,
            requires_solution=requires_solution,
        )
    except assignments.AssignmentError as exc:
        return _redirect("/teacher/assignments", error=str(exc))

    if len(created) > 1:
        return _redirect(
            "/teacher/assignments",
            message=_("Задание выдано группам: %(count)s") % {"count": len(created)},
        )
    return _redirect(
        f"/teacher/assignments/{created[0].id}",
        message=_("Задание выдано и отправлено в Telegram")
        if delivered
        else _("Задание выдано"),
    )


@router.get("/assignments/{assignment_id}")
async def assignment_matrix(
    request: Request, session: SessionDep, user: TeacherUser, assignment_id: int
):
    assignment = await session.get(Assignment, assignment_id)
    if assignment is None:
        return _redirect("/teacher/assignments", error=_("Задание не найдено"))

    participants = await participants_for_assignment(session, assignment)
    progress = await compute_progress(session, assignment, participants)

    files: dict[int, solutions.Tally] = {}
    files_by_problem: dict[int, int] = {}
    uploads: dict[tuple[int, int], SolutionUpload] = {}
    if assignment.requires_solution:
        uploads = await solutions.for_assignment(
            session, assignment.id, [p.id for p in participants]
        )
        problem_ids = [p.id for p in progress.problems]
        files = {
            student.id: solutions.tally(uploads, student.id, problem_ids)
            for student in participants
        }
        # Сколько файлов прислано по каждой задаче: видно, что просели именно
        # на ней, а не «у кого-то что-то не сдано».
        files_by_problem = {
            problem_id: sum(
                1 for student in participants if (student.id, problem_id) in uploads
            )
            for problem_id in problem_ids
        }

    # Когда решали: посылки участников по времени окна задания.
    built = await timeline.build(
        session, assignment, participants, progress.problems, LOCAL_TZ
    )
    chart = share = solves_on_time = None
    if built is not None:
        when, in_time = built
        chart = timeline.layout(when, LOCAL_TZ)
        share = when.last_day_share(in_time)
        solves_on_time = len(in_time)

    return templates.TemplateResponse(
        request,
        "teacher/assignment.html",
        {
            "user": user,
            "assignment": assignment,
            "progress": progress,
            "chart": chart,
            "last_day_share": share,
            "solves_on_time": solves_on_time,
            "files": files,
            "files_by_problem": files_by_problem,
            "uploads": uploads,
            "messages": await messages.for_assignment(session, assignment.id),
            # Список общий: убранная задача пропадёт и из этих заданий.
            "siblings": await _all(
                session,
                select(Assignment).where(
                    Assignment.problem_set_id == assignment.problem_set_id,
                    Assignment.id != assignment.id,
                ),
            ),
            **_flash(request),
        },
    )


@router.post("/assignments/{assignment_id}/problems/{problem_id}/remove")
async def remove_assignment_problem(
    session: SessionDep, user: TeacherUser, assignment_id: int, problem_id: int
):
    """Убрать задачу из уже выданного задания. Убирается из списка, поэтому
    и из других заданий по нему — об этом предупреждает форма."""
    assignment = await session.get(Assignment, assignment_id)
    if assignment is None:
        return _redirect("/teacher/assignments", error=_("Задание не найдено"))
    removed = await problem_sets.remove(session, assignment.problem_set_id, [problem_id])
    back = f"/teacher/assignments/{assignment_id}"
    if not removed:
        return _redirect(back, error=_("Этой задачи в задании уже нет"))
    return _redirect(
        back, message=_("Задача убрана; посылки студентов сохранены, зачёт пересчитан")
    )


@router.post("/assignments/{assignment_id}/messages")
async def post_message(
    request: Request,
    session: SessionDep,
    user: TeacherUser,
    assignment_id: int,
    body: Annotated[str, Form()] = "",
    telegram: Annotated[bool, Form()] = False,
):
    """Сообщение участникам задания. Возвращаемся туда, откуда писали:
    со страницы студента — на неё, из матрицы — в матрицу."""
    assignment = await session.get(Assignment, assignment_id)
    if assignment is None:
        return _redirect("/teacher/assignments", error=_("Задание не найдено"))
    back = _message_back(request, assignment_id)
    result = await messages.post(session, assignment, user, body, telegram=telegram)
    if isinstance(result, str):
        return RedirectResponse(f"{back}?err={quote(result)}#messages", status_code=303)
    return RedirectResponse(f"{back}#messages", status_code=303)


def _message_back(request: Request, assignment_id: int) -> str:
    # Путь сравниваем целиком: «/teacher/assignments/5» тоже кончается на
    # «/assignments/5», и из матрицы уводило бы на страницу студента.
    student_page = f"/assignments/{assignment_id}"
    if urlparse(request.headers.get("referer", "")).path == student_page:
        return student_page
    return f"/teacher/assignments/{assignment_id}"


@router.post("/assignments/{assignment_id}/messages/{message_id}/delete")
async def delete_message(
    request: Request, session: SessionDep, user: TeacherUser, assignment_id: int, message_id: int
):
    await messages.delete(session, assignment_id, message_id)
    return RedirectResponse(f"{_message_back(request, assignment_id)}#messages", status_code=303)


@router.get("/assignments/{assignment_id}/similar")
async def similar_solutions(
    request: Request,
    session: SessionDep,
    user: TeacherUser,
    assignment_id: int,
    scope: str = "group",
):
    """Похожие решения по задачам задания — отчёт после контрольной.
    По умолчанию — внутри группы; `scope=all` — ещё и с другими группами."""
    assignment = await session.get(Assignment, assignment_id)
    if assignment is None:
        return _redirect("/teacher/assignments", error=_("Задание не найдено"))
    across = scope == "all"
    reports = await similarity.for_assignment(session, assignment, across_groups=across)
    return templates.TemplateResponse(
        request,
        "teacher/similar.html",
        {
            "user": user,
            "assignment": assignment,
            "reports": reports,
            "across": across,
            "threshold": round(similarity.THRESHOLD * 100),
            "across_threshold": round(similarity.ACROSS_THRESHOLD * 100),
            "min_tokens": similarity.MIN_TOKENS,
        },
    )


def _highlighted(found: solutions.Picked) -> str:
    if found.source == "upload":
        return notebook.highlight_file(found.code, solutions.FILENAME)
    return notebook.highlight(found.code, languages.lexer_name(found.language))


@router.get("/assignments/{assignment_id}/similar/{problem_id}")
async def similar_pair(
    request: Request,
    session: SessionDep,
    user: TeacherUser,
    assignment_id: int,
    problem_id: int,
    a: int,
    b: int,
):
    """Два решения рядом — чтобы решать глазами, а не по процентам."""
    assignment = await session.get(Assignment, assignment_id)
    if assignment is None:
        return _redirect("/teacher/assignments", error=_("Задание не найдено"))
    back = f"/teacher/assignments/{assignment_id}/similar"
    # С другими группами — это все пары, и внутри группы тоже.
    reports = await similarity.for_assignment(session, assignment, problem_id, across_groups=True)
    match = next(
        (
            m for r in reports for m in r.matches
            if {m.first.id, m.second.id} == {a, b}
        ),
        None,
    )
    if match is None:
        return _redirect(back, error=_("Этой пары нет среди похожих решений"))
    return templates.TemplateResponse(
        request,
        "teacher/similar_pair.html",
        {
            "user": user,
            "assignment": assignment,
            "problem": reports[0].problem,
            "match": match,
            "sides": [
                (match.first, match.first_solution, _highlighted(match.first_solution),
                 match.first_from),
                (match.second, match.second_solution, _highlighted(match.second_solution),
                 match.second_from),
            ],
        },
    )


@router.get("/assignments/{assignment_id}/export.csv")
async def export_assignment(session: SessionDep, user: TeacherUser, assignment_id: int):
    assignment = await session.get(Assignment, assignment_id)
    if assignment is None:
        return _redirect("/teacher/assignments", error=_("Задание не найдено"))

    participants = await participants_for_assignment(session, assignment)
    progress = await compute_progress(session, assignment, participants)
    name = export.filename(f"assignment-{assignment.id}", utcnow())
    return Response(
        export.assignment_csv(assignment, progress),
        media_type="text/csv; charset=utf-8",
        headers=export.response_headers(name),
    )


@router.get("/export/leaderboard.csv")
async def export_leaderboard(
    session: SessionDep, user: TeacherUser, group_id: OptionalInt = None, period: str = "all"
):
    if group_id is None:
        return _redirect("/leaderboard", error=_("Выбери группу: общего табло нет"))
    days = PERIODS.get(period, PERIODS["all"])[1]
    since = utcnow() - timedelta(days=days) if days else None
    rows = await build_leaderboard(session, group_id=group_id, since=since)
    name = export.filename("leaderboard", utcnow())
    return Response(
        await export.leaderboard_csv(session, rows),
        media_type="text/csv; charset=utf-8",
        headers=export.response_headers(name),
    )


@router.post("/assignments/{assignment_id}/edit")
async def edit_assignment(
    session: SessionDep,
    user: TeacherUser,
    assignment_id: int,
    title: str = Form(...),
    description: str = Form(""),
):
    """Что правится после выдачи — решает `assignments.edit`; срок двигается
    отдельной формой (`shift_deadline`)."""
    assignment = await session.get(Assignment, assignment_id)
    if assignment is None:
        return _redirect("/teacher/assignments", error=_("Задание не найдено"))
    try:
        await assignments.edit(session, assignment, title=title, description=description)
    except assignments.AssignmentError as exc:
        return _redirect(f"/teacher/assignments/{assignment_id}", error=str(exc))
    return _redirect(f"/teacher/assignments/{assignment_id}", message=_("Сохранено"))


@router.post("/assignments/{assignment_id}/deadline")
async def shift_deadline(
    session: SessionDep,
    user: TeacherUser,
    assignment_id: int,
    deadline: str = Form(""),
    remove: bool = Form(False),
):
    """Отдельно от правки названия: перенос срока меняет зачёт у всех участников.

    Двигается только вперёд — см. `extend_deadline`.
    """
    assignment = await session.get(Assignment, assignment_id)
    if assignment is None:
        return _redirect("/teacher/assignments", error=_("Задание не найдено"))
    back = f"/teacher/assignments/{assignment_id}"
    new_deadline = None
    if not remove:
        new_deadline = parse_local_input(deadline)
        if new_deadline is None:
            return _redirect(back, error=_("Укажи новый дедлайн"))
    error = extend_deadline(assignment, new_deadline)
    if error:
        return _redirect(back, error=error)
    await session.commit()
    return _redirect(
        back, message=_("Дедлайн снят") if new_deadline is None else _("Дедлайн сдвинут")
    )


@router.post("/assignments/{assignment_id}/delete")
async def delete_assignment(session: SessionDep, user: TeacherUser, assignment_id: int):
    assignment = await session.get(Assignment, assignment_id)
    if assignment is not None:
        await session.delete(assignment)
        await session.commit()
    return _redirect("/teacher/assignments", message=_("Задание удалено"))


# ---------------------------------------------------------------- объявления


@router.get("/announcements")
async def announcements_page(request: Request, session: SessionDep, user: TeacherUser):
    items = await _all(
        session,
        select(Announcement).order_by(Announcement.pinned.desc(), Announcement.created_at.desc()),
    )
    groups = await _all(
        session, select(Group).where(Group.is_archived.is_(False)).order_by(Group.title)
    )
    return templates.TemplateResponse(
        request,
        "teacher/announcements.html",
        {"user": user, "items": items, "groups": groups, "now_ts": utcnow(), **_flash(request)},
    )


@router.post("/announcements")
async def create_announcement(
    session: SessionDep,
    user: TeacherUser,
    title: str = Form(...),
    body: str = Form(""),
    url: str = Form(""),
    url_label: str = Form(""),
    group_id: str = Form(""),
    starts_at: str = Form(""),
    ends_at: str = Form(""),
    pinned: bool = Form(False),
):
    try:
        _item, delivered = await announcements.publish(
            session,
            user,
            title=title,
            body=body,
            url=url,
            url_label=url_label,
            group_id=int(group_id) if group_id.strip() else None,
            starts_at=parse_local_input(starts_at),
            ends_at=parse_local_input(ends_at),
            pinned=pinned,
        )
    except announcements.AnnouncementError as exc:
        return _redirect("/teacher/announcements", error=str(exc))
    return _redirect(
        "/teacher/announcements",
        message=_("Объявление опубликовано и отправлено в Telegram")
        if delivered
        else _("Объявление опубликовано"),
    )


@router.post("/announcements/{announcement_id}/pin")
async def toggle_pin(session: SessionDep, user: TeacherUser, announcement_id: int):
    item = await session.get(Announcement, announcement_id)
    if item is None:
        return _redirect("/teacher/announcements", error=_("Объявление не найдено"))
    item.pinned = not item.pinned
    await session.commit()
    return _redirect("/teacher/announcements", message=_("Готово"))


@router.post("/announcements/{announcement_id}/delete")
async def delete_announcement(session: SessionDep, user: TeacherUser, announcement_id: int):
    item = await session.get(Announcement, announcement_id)
    if item is not None:
        await session.delete(item)
        await session.commit()
    return _redirect("/teacher/announcements", message=_("Объявление удалено"))


# ---------------------------------------------------------------- студенты


@router.get("/students")
async def students_page(
    request: Request, session: SessionDep, user: TeacherUser, role: str = "student"
):
    """Две вкладки: студенты и преподаватели.

    В общем списке преподаватели терялись среди сотни студентов, а нужны они
    ровно тогда, когда кому-то выдают или снимают роль.
    """
    role = "teacher" if role == "teacher" else "student"
    wanted = Role.teacher if role == "teacher" else Role.student
    students = await _all(
        session, select(User).where(User.role == wanted).order_by(User.display_name)
    )
    tabs = {
        "student": await session.scalar(
            select(func.count()).select_from(User).where(User.role == Role.student)
        ),
        "teacher": await session.scalar(
            select(func.count()).select_from(User).where(User.role == Role.teacher)
        ),
    }
    solved = dict(
        (
            await session.execute(
                select(Submission.user_id, func.count(func.distinct(Submission.problem_id)))
                .where(Submission.is_accepted.is_(True))
                .group_by(Submission.user_id)
            )
        ).all()
    )
    bonuses = dict(
        (
            await session.execute(
                select(BonusPoint.user_id, func.sum(BonusPoint.points)).group_by(
                    BonusPoint.user_id
                )
            )
        ).all()
    )
    return templates.TemplateResponse(
        request,
        "teacher/students.html",
        {
            "user": user,
            "students": students,
            "role": role,
            "tabs": tabs,
            "solved": solved,
            "bonuses": bonuses,
            **_flash(request),
        },
    )


@router.post("/students/{user_id}/bonus")
async def grant_bonus(
    session: SessionDep,
    user: TeacherUser,
    user_id: int,
    points: str = Form(""),
    reason: str = Form(""),
):
    """Баллы приходят строкой: пустое поле и запятая в дробной части —
    обычный ввод, а не повод показать страницу с ошибкой."""
    target = await session.get(User, user_id)
    if target is None:
        return _redirect("/teacher/students", error=_("Студент не найден"))
    try:
        amount = float(points.strip().replace(",", "."))
    except ValueError:
        return _redirect("/teacher/students", error=_("Баллы — это число"))
    if amount == 0:
        return _redirect("/teacher/students", error=_("Ноль баллов начислять нечего"))
    session.add(
        BonusPoint(
            user_id=user_id,
            points=amount,
            reason=reason.strip() or _("без комментария"),
            granted_by_id=user.id,
            granted_at=utcnow(),
        )
    )
    await session.commit()
    return _redirect("/teacher/students", message=_("Баллы начислены"))


@router.post("/students/{user_id}/role")
async def toggle_role(session: SessionDep, user: AdminUser, user_id: int):
    target = await session.get(User, user_id)
    if target is None:
        return _redirect("/teacher/students", error=_("Студент не найден"))
    if target.id == user.id:
        return _redirect("/teacher/students", error=_("Нельзя снять роль с себя"))
    target.role = Role.student if target.role == Role.teacher else Role.teacher
    await session.commit()
    return _redirect("/teacher/students", message=_("Роль изменена"))


@router.get("/problems/unlinked")
async def unlinked_submissions(request: Request, session: SessionDep, user: AdminUser):
    """Посылки по задачам, которых нет в каталоге — обычно свежие контесты."""
    rows = list(
        (
            await session.execute(
                select(
                    Submission.platform,
                    Submission.problem_slug,
                    Submission.problem_title,
                    func.count(),
                )
                .where(Submission.problem_id.is_(None))
                .group_by(Submission.platform, Submission.problem_slug, Submission.problem_title)
                .order_by(func.count().desc())
                .limit(100)
            )
        ).all()
    )
    return templates.TemplateResponse(
        request, "teacher/unlinked.html", {"user": user, "rows": rows}
    )
