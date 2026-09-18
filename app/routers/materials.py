"""Раздел «Материалы»: преподаватель публикует файлы, студенты скачивают."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select

from app import notify
from app.deps import CurrentUser, SessionDep, TeacherUser
from app.i18n import translate as _
from app.models import Group, Material
from app.services import materials, notebook
from app.services.progress import groups_for_user
from app.templating import templates

router = APIRouter(tags=["materials"])


def _back(message: str | None = None, error: str | None = None) -> RedirectResponse:
    params = []
    if message:
        params.append("ok=" + quote(message))
    if error:
        params.append("err=" + quote(error))
    suffix = ("?" + "&".join(params)) if params else ""
    return RedirectResponse(f"/materials{suffix}", status_code=303)


async def _visible_to(session: SessionDep, user) -> list[Material]:
    stmt = select(Material).order_by(Material.published_at.desc())
    if not user.is_teacher:
        # Материал без группы — общий для всех, остальные только своей группе.
        mine = [g.id for g in await groups_for_user(session, user)]
        stmt = stmt.where(Material.group_id.is_(None) | Material.group_id.in_(mine))
    return list((await session.execute(stmt)).scalars().all())


@router.get("/materials")
async def materials_page(request: Request, session: SessionDep, user: CurrentUser):
    groups = []
    if user.is_teacher:
        groups = list(
            (
                await session.execute(
                    select(Group).where(Group.is_archived.is_(False)).order_by(Group.title)
                )
            ).scalars().all()
        )
    return templates.TemplateResponse(
        request,
        "materials.html",
        {
            "user": user,
            "items": await _visible_to(session, user),
            "groups": groups,
            "extensions": materials.EXTENSIONS_HINT,
            "max_mb": materials.MAX_BYTES // 1024 // 1024,
            "ok": request.query_params.get("ok"),
            "error": request.query_params.get("err"),
        },
    )


@router.post("/materials")
async def publish_material(
    session: SessionDep,
    user: TeacherUser,
    file: UploadFile = File(...),
    title: str = Form(""),
    description: str = Form(""),
    group_id: str = Form(""),
):
    name = materials.safe_filename(file.filename or "")
    if not materials.is_allowed(name):
        return _back(
            error=_("Такой файл не принимаем. Можно: %(list)s")
            % {"list": materials.EXTENSIONS_HINT}
        )

    data = await file.read()
    if not data:
        return _back(error=_("Файл пустой"))
    if len(data) > materials.MAX_BYTES:
        return _back(error=_("Файл больше %(mb)s МБ") % {"mb": materials.MAX_BYTES // 1024 // 1024})

    target_group = None
    if group_id.strip():
        group = await session.get(Group, int(group_id))
        if group is None:
            return _back(error=_("Группа не найдена"))
        target_group = group.id

    stored = materials.new_stored_name(name)
    materials.save(stored, data)
    item = Material(
        title=title.strip() or name,
        description=description.strip() or None,
        group_id=target_group,
        stored_name=stored,
        filename=name,
        content_type=materials.content_type_for(name),
        size=len(data),
        created_by_id=user.id,
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)

    delivered = await notify.notify_material(item, session)
    return _back(
        message=_("Материал опубликован и отправлен в Telegram")
        if delivered
        else _("Материал опубликован")
    )


@router.get("/materials/{material_id}/download")
async def download_material(session: SessionDep, user: CurrentUser, material_id: int):
    item = await session.get(Material, material_id)
    if item is None or item not in await _visible_to(session, user):
        return _back(error=_("Материал не найден"))
    path = materials.path_for(item.stored_name)
    if not path.is_file():
        return _back(error=_("Файл потерялся на диске — попроси выложить заново"))
    # Именно скачивание: открывать чужой файл на своём домене не нужно.
    return FileResponse(
        path,
        media_type=item.content_type,
        filename=item.filename,
        content_disposition_type="attachment",
    )


@router.get("/materials/{material_id}/view")
async def view_material(request: Request, session: SessionDep, user: CurrentUser, material_id: int):
    item = await session.get(Material, material_id)
    if item is None or item not in await _visible_to(session, user):
        return _back(error=_("Материал не найден"))
    if not notebook.is_previewable(item.filename, item.size):
        return _back(error=_("Этот файл можно только скачать"))

    path = materials.path_for(item.stored_name)
    if not path.is_file():
        return _back(error=_("Файл потерялся на диске — попроси выложить заново"))
    content = path.read_bytes()

    name = item.filename.lower()
    cells = notebook.parse(content) if name.endswith(".ipynb") else None
    text = rendered = None
    if cells is None:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            return _back(error=_("Файл не читается как текст — скачай его"))
        if name.endswith(".md"):
            rendered, text = notebook.render_markdown(text), None

    return templates.TemplateResponse(
        request,
        "material.html",
        {"user": user, "item": item, "cells": cells, "text": text, "rendered": rendered},
    )


@router.post("/materials/{material_id}/delete")
async def delete_material(session: SessionDep, user: TeacherUser, material_id: int):
    item = await session.get(Material, material_id)
    if item is None:
        return _back(error=_("Материал не найден"))
    materials.remove(item.stored_name)
    await session.delete(item)
    await session.commit()
    return _back(message=_("Материал удалён"))
