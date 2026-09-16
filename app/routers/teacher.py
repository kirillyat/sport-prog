from __future__ import annotations

import secrets
import string
from datetime import datetime, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import notify
from app.deps import OptionalInt, SessionDep, TeacherUser
from app.models import (
    Announcement,
    Assignment,
    BonusPoint,
    Group,
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
from app.services import export, features, solutions
from app.services.catalog import get_state, problem_count, sync_catalog
from app.services.feed import build_feed
from app.services.leaderboard import build_leaderboard
from app.services.problem_parser import parse_problem_list, search_problems
from app.services.progress import compute_progress, participants_for_assignment
from app.services.sync import relink_orphan_submissions, sync_all
from app.templating import parse_local_input, templates

router = APIRouter(prefix="/teacher", tags=["teacher"])

JOIN_ALPHABET = string.ascii_uppercase + string.digits


def _redirect(path: str, message: str | None = None, error: str | None = None):
    params = []
    if message:
        params.append(f"ok={message}")
    if error:
        params.append(f"err={error}")
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
    catalog = {p.title: await problem_count(session, p) for p in Platform}
    # Пока курс не собран целиком, панель показывает шаги, а не нули в плитках.
    steps = [
        {"done": bool(counts["groups"]), "title": "Создать группу",
         "hint": "И раздать студентам код вступления", "href": "/teacher/groups"},
        {"done": bool(counts["sets"]), "title": "Собрать список задач",
         "hint": "Вставить ссылки на задачи текстом", "href": "/teacher/sets"},
        {"done": bool(counts["assignments"]), "title": "Выдать задание",
         "hint": "Назначить список группе с дедлайном", "href": "/teacher/assignments"},
    ]
    return templates.TemplateResponse(
        request,
        "teacher/overview.html",
        {
            "user": user,
            "counts": counts,
            "steps": steps,
            "onboarding": not all(step["done"] for step in steps),
            "catalog": catalog,
            "catalog_synced_at": _parse_iso(await get_state(session, "catalog_synced_at")),
            **_flash(request),
        },
    )


@router.post("/sync-now")
async def sync_now(session: SessionDep, user: TeacherUser):
    added = await sync_all(session)
    return _redirect("/teacher", message=f"Синхронизация+завершена,+новых+посылок:+{added}")


@router.post("/catalog-refresh")
async def catalog_refresh(session: SessionDep, user: TeacherUser):
    counts = await sync_catalog(session)
    fixed = await relink_orphan_submissions(session)
    total = sum(counts.values())
    return _redirect(
        "/teacher", message=f"Каталог+обновлён:+{total}+задач,+подвязано+посылок:+{fixed}"
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
        return _redirect("/teacher/groups", error="Пустое+название")
    group = Group(title=title, join_code=await _new_join_code(session), created_by_id=user.id)
    session.add(group)
    await session.commit()
    return _redirect("/teacher/groups", message=f"Группа+«{title}»+создана")


@router.get("/groups/{group_id}")
async def group_detail(request: Request, session: SessionDep, user: TeacherUser, group_id: int):
    group = await session.get(Group, group_id)
    if group is None:
        return _redirect("/teacher/groups", error="Группа+не+найдена")
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
        return _redirect("/teacher/groups", error="Группа+не+найдена")
    chat = chat_id.strip()
    if chat and not chat.lstrip("-").isdigit():
        return _redirect(
            f"/teacher/groups/{group_id}", error="Id+чата+—+число,+например+-1001234567890"
        )
    group.telegram_chat_id = chat or None
    await session.commit()
    message = "Чат+группы+сохранён" if chat else "Чат+группы+отвязан"
    return _redirect(f"/teacher/groups/{group_id}", message=message)


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
        return _redirect("/teacher/groups", error="Группа+не+найдена")

    student = await session.get(User, user_id)
    if student is None or not student.is_active:
        return _redirect(f"/teacher/groups/{group_id}", error="Студент+не+найден")
    if student.is_teacher:
        return _redirect(f"/teacher/groups/{group_id}", error="Преподавателя+в+группу+не+добавляем")

    existing = await session.scalar(
        select(GroupMembership).where(
            GroupMembership.group_id == group_id, GroupMembership.user_id == user_id
        )
    )
    if existing is None:
        session.add(GroupMembership(group_id=group_id, user_id=user_id))
        await session.commit()
    name = quote(student.display_name)
    return _redirect(f"/teacher/groups/{group_id}", message=f"{name}+в+группе")


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
    return _redirect(f"/teacher/groups/{group_id}", message="Студент+исключён")


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
async def features_page(request: Request, session: SessionDep, user: TeacherUser):
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
async def save_features(request: Request, session: SessionDep, user: TeacherUser):
    """Галочки приходят только для включённых — остальное считаем выключенным."""
    form = await request.form()
    for section in features.SECTIONS:
        await features.save(
            session,
            section.key,
            for_students=f"{section.key}:students" in form,
            for_teachers=f"{section.key}:teachers" in form,
        )
    return _redirect("/teacher/features", message="Видимость+разделов+сохранена")


@router.post("/groups/{group_id}/archive")
async def archive_group(session: SessionDep, user: TeacherUser, group_id: int):
    group = await session.get(Group, group_id)
    if group is None:
        return _redirect("/teacher/groups", error="Группа+не+найдена")
    group.is_archived = not group.is_archived
    await session.commit()
    return _redirect("/teacher/groups", message="Готово")


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
    title = title.strip()
    if not title:
        return _redirect("/teacher/sets", error="Пустое+название")

    problem_set = ProblemSet(
        title=title, description=description.strip() or None, created_by_id=user.id
    )
    session.add(problem_set)
    await session.commit()
    await session.refresh(problem_set)

    if problems_text.strip():
        result = await parse_problem_list(session, problems_text)
        for position, problem in enumerate(result.problems):
            session.add(
                ProblemSetItem(
                    problem_set_id=problem_set.id, problem_id=problem.id, position=position
                )
            )
        await session.commit()
        if result.unresolved:
            return _redirect(
                f"/teacher/sets/{problem_set.id}",
                error="Не+распознано:+" + ",+".join(result.unresolved[:5]),
            )
    return _redirect(f"/teacher/sets/{problem_set.id}", message="Список+создан")


@router.get("/sets/{set_id}")
async def set_detail(request: Request, session: SessionDep, user: TeacherUser, set_id: int):
    problem_set = await session.get(ProblemSet, set_id)
    if problem_set is None:
        return _redirect("/teacher/sets", error="Список+не+найден")
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
        return _redirect("/teacher/sets", error="Список+не+найден")

    existing = {item.problem_id for item in problem_set.items}
    next_position = max((item.position for item in problem_set.items), default=-1) + 1

    result = await parse_problem_list(session, problems_text)
    added = 0
    for problem in result.problems:
        if problem.id in existing:
            continue
        session.add(
            ProblemSetItem(
                problem_set_id=set_id, problem_id=problem.id, position=next_position + added
            )
        )
        added += 1
    await session.commit()

    if result.unresolved:
        return _redirect(
            f"/teacher/sets/{set_id}",
            error=f"Добавлено+{added}.+Не+распознано:+" + ",+".join(result.unresolved[:5]),
        )
    return _redirect(f"/teacher/sets/{set_id}", message=f"Добавлено+задач:+{added}")


@router.post("/sets/{set_id}/remove/{item_id}")
async def remove_problem(session: SessionDep, user: TeacherUser, set_id: int, item_id: int):
    item = await session.get(ProblemSetItem, item_id)
    if item is not None and item.problem_set_id == set_id:
        await session.delete(item)
        await session.commit()
    return _redirect(f"/teacher/sets/{set_id}", message="Задача+убрана")


@router.post("/sets/{set_id}/delete")
async def delete_set(session: SessionDep, user: TeacherUser, set_id: int):
    problem_set = await session.get(ProblemSet, set_id)
    if problem_set is None:
        return _redirect("/teacher/sets", error="Список+не+найден")
    in_use = await session.scalar(
        select(func.count()).select_from(Assignment).where(Assignment.problem_set_id == set_id)
    )
    if in_use:
        return _redirect("/teacher/sets", error="Список+используется+в+заданиях")
    await session.delete(problem_set)
    await session.commit()
    return _redirect("/teacher/sets", message="Список+удалён")


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
            "platforms": list(Platform),
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
    group_id: str = Form(""),
    description: str = Form(""),
    starts_at: str = Form(""),
    deadline: str = Form(""),
    hard_deadline: bool = Form(False),
    count_prior_solves: bool = Form(False),
    requires_solution: bool = Form(False),
):
    """Все правила задаются здесь: потом меняются только название и описание."""
    title = title.strip()
    if not title:
        return _redirect("/teacher/assignments", error="Пустое+название")
    if await session.get(ProblemSet, problem_set_id) is None:
        return _redirect("/teacher/assignments", error="Список+задач+не+найден")

    target_group = int(group_id) if group_id.strip() else None
    if target_group is not None and await session.get(Group, target_group) is None:
        return _redirect("/teacher/assignments", error="Группа+не+найдена")

    start = parse_local_input(starts_at) or utcnow()
    end = parse_local_input(deadline)
    if end is not None and end <= start:
        return _redirect("/teacher/assignments", error="Дедлайн+раньше+начала")

    if hard_deadline and end is None:
        return _redirect("/teacher/assignments", error="Жёсткий+дедлайн+без+даты+не+работает")

    assignment = Assignment(
        title=title,
        description=description.strip() or None,
        problem_set_id=problem_set_id,
        group_id=target_group,
        assigned_at=start,
        deadline=end,
        hard_deadline=hard_deadline,
        created_by_id=user.id,
        count_prior_solves=count_prior_solves,
        requires_solution=requires_solution,
    )
    session.add(assignment)
    await session.commit()
    await session.refresh(assignment)
    # После refresh связи не загружены — считаем задачи отдельным запросом.
    problems = await session.scalar(
        select(func.count()).select_from(ProblemSetItem).where(
            ProblemSetItem.problem_set_id == problem_set_id
        )
    )
    delivered = await notify.notify_assignment(assignment, int(problems or 0), session)
    suffix = "+и+отправлено+в+Telegram" if delivered else ""
    return _redirect(f"/teacher/assignments/{assignment.id}", message="Задание+выдано" + suffix)


@router.get("/assignments/{assignment_id}")
async def assignment_matrix(
    request: Request, session: SessionDep, user: TeacherUser, assignment_id: int
):
    assignment = await session.get(Assignment, assignment_id)
    if assignment is None:
        return _redirect("/teacher/assignments", error="Задание+не+найдено")

    participants = await participants_for_assignment(session, assignment)
    progress = await compute_progress(session, assignment, participants)

    files: dict[int, solutions.Tally] = {}
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

    return templates.TemplateResponse(
        request,
        "teacher/assignment.html",
        {
            "user": user,
            "assignment": assignment,
            "progress": progress,
            "files": files,
            "uploads": uploads,
            **_flash(request),
        },
    )


@router.get("/assignments/{assignment_id}/export.csv")
async def export_assignment(session: SessionDep, user: TeacherUser, assignment_id: int):
    assignment = await session.get(Assignment, assignment_id)
    if assignment is None:
        return _redirect("/teacher/assignments", error="Задание+не+найдено")

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
        return _redirect("/leaderboard", error="Выбери+группу:+общего+табло+нет")
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
    """Правится только то, что не меняет подсчёт: название и описание.

    Сроки, список задач, адресат и баллы задаются при создании — иначе
    рейтинг задним числом менялся бы у всех участников.
    """
    assignment = await session.get(Assignment, assignment_id)
    if assignment is None:
        return _redirect("/teacher/assignments", error="Задание+не+найдено")
    title = title.strip()
    if not title:
        return _redirect(f"/teacher/assignments/{assignment_id}", error="Пустое+название")
    assignment.title = title
    assignment.description = description.strip() or None
    await session.commit()
    return _redirect(f"/teacher/assignments/{assignment_id}", message="Сохранено")


@router.post("/assignments/{assignment_id}/delete")
async def delete_assignment(session: SessionDep, user: TeacherUser, assignment_id: int):
    assignment = await session.get(Assignment, assignment_id)
    if assignment is not None:
        await session.delete(assignment)
        await session.commit()
    return _redirect("/teacher/assignments", message="Задание+удалено")


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
    title = title.strip()
    if not title:
        return _redirect("/teacher/announcements", error="Пустой+заголовок")

    link = url.strip()
    if link and not link.startswith(("http://", "https://")):
        return _redirect("/teacher/announcements", error="Ссылка+должна+начинаться+с+http")

    start = parse_local_input(starts_at)
    end = parse_local_input(ends_at)
    if start and end and end <= start:
        return _redirect("/teacher/announcements", error="Конец+раньше+начала")

    item = Announcement(
        title=title,
        body=body.strip() or None,
        url=link or None,
        url_label=url_label.strip() or None,
        group_id=int(group_id) if group_id.strip() else None,
        starts_at=start,
        ends_at=end,
        pinned=pinned,
        created_by_id=user.id,
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    delivered = await notify.notify_announcement(item, session)
    suffix = "+и+отправлено+в+Telegram" if delivered else ""
    return _redirect("/teacher/announcements", message="Объявление+опубликовано" + suffix)


@router.post("/announcements/{announcement_id}/pin")
async def toggle_pin(session: SessionDep, user: TeacherUser, announcement_id: int):
    item = await session.get(Announcement, announcement_id)
    if item is None:
        return _redirect("/teacher/announcements", error="Объявление+не+найдено")
    item.pinned = not item.pinned
    await session.commit()
    return _redirect("/teacher/announcements", message="Готово")


@router.post("/announcements/{announcement_id}/delete")
async def delete_announcement(session: SessionDep, user: TeacherUser, announcement_id: int):
    item = await session.get(Announcement, announcement_id)
    if item is not None:
        await session.delete(item)
        await session.commit()
    return _redirect("/teacher/announcements", message="Объявление+удалено")


# ---------------------------------------------------------------- студенты


@router.get("/students")
async def students_page(request: Request, session: SessionDep, user: TeacherUser):
    students = await _all(session, select(User).order_by(User.display_name))
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
        return _redirect("/teacher/students", error="Студент+не+найден")
    try:
        amount = float(points.strip().replace(",", "."))
    except ValueError:
        return _redirect("/teacher/students", error="Баллы+—+это+число")
    if amount == 0:
        return _redirect("/teacher/students", error="Ноль+баллов+начислять+нечего")
    session.add(
        BonusPoint(
            user_id=user_id,
            points=amount,
            reason=reason.strip() or "без комментария",
            granted_by_id=user.id,
            granted_at=utcnow(),
        )
    )
    await session.commit()
    return _redirect("/teacher/students", message="Баллы+начислены")


@router.post("/students/{user_id}/role")
async def toggle_role(session: SessionDep, user: TeacherUser, user_id: int):
    target = await session.get(User, user_id)
    if target is None:
        return _redirect("/teacher/students", error="Студент+не+найден")
    if target.id == user.id:
        return _redirect("/teacher/students", error="Нельзя+снять+роль+с+себя")
    target.role = Role.student if target.role == Role.teacher else Role.teacher
    await session.commit()
    return _redirect("/teacher/students", message="Роль+изменена")


@router.get("/problems/unlinked")
async def unlinked_submissions(request: Request, session: SessionDep, user: TeacherUser):
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
