"""Раздел «ИИ»: как подключить свою нейросеть к порталу по MCP и ключи для неё.

Пока здесь только подключение внешних моделей. Список инструментов на
странице берётся из того же реестра, что отвечает по MCP, — описание
и поведение не разъедутся.
"""

from __future__ import annotations

import json
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import settings
from app.deps import CurrentUser, SessionDep, viewing_as_student
from app.i18n import translate as _
from app.mcp import registry
from app.mcp import server as _server  # noqa: F401 — регистрирует инструменты
from app.models import Role, User
from app.services import api_tokens, features
from app.templating import templates

router = APIRouter(tags=["ai"])

SERVER_NAME = "portal"
PLACEHOLDER = "<KEY>"


def endpoint() -> str:
    return settings.base_url.rstrip("/") + "/mcp"


def snippets(token: str) -> list[dict[str, str]]:
    """Готовые настройки для распространённых клиентов. С только что выпущенным
    ключом — сразу с ним, иначе с заглушкой на его месте."""
    url = endpoint()
    cursor = {"mcpServers": {SERVER_NAME: {"url": url, "headers": {
        "Authorization": f"Bearer {token}"}}}}
    # Claude Desktop не умеет передавать заголовок сам — через мост mcp-remote.
    # Пробела после двоеточия нет нарочно: в Windows он ломает аргумент.
    desktop = {"mcpServers": {SERVER_NAME: {
        "command": "npx",
        "args": ["-y", "mcp-remote", url, "--header", "Authorization:${PORTAL_AUTH}"],
        "env": {"PORTAL_AUTH": f"Bearer {token}"},
    }}}
    return [
        {
            "client": "Claude Code",
            "where": _("команда в терминале"),
            "code": f'claude mcp add --transport http {SERVER_NAME} {url} '
            f'--header "Authorization: Bearer {token}"',
        },
        {
            "client": "Codex",
            "where": "~/.codex/config.toml",
            "code": f'[mcp_servers.{SERVER_NAME}]\nurl = "{url}"\n'
            f'bearer_token_env_var = "PORTAL_TOKEN"\n\n'
            f"# {_('в окружении, откуда запускается codex:')}\n"
            f"# export PORTAL_TOKEN={token}",
        },
        {
            "client": "Cursor",
            "where": ".cursor/mcp.json",
            "code": json.dumps(cursor, ensure_ascii=False, indent=2),
        },
        {
            "client": "Claude Desktop",
            "where": "claude_desktop_config.json",
            "code": json.dumps(desktop, ensure_ascii=False, indent=2),
        },
    ]


async def _page(request: Request, session: SessionDep, user: User, issued: str | None):
    # Преподаватель, смотрящий глазами студента, видит студенческую страницу:
    # иначе не проверить, что студенту обещано и что ему доступно. Ключей
    # в этом виде не выпускаем — ключ всегда действует с настоящей ролью,
    # и «студенческий» ключ преподавателя отдал бы его инструменты.
    preview = user.is_teacher and viewing_as_student(request)
    teacher_page = user.is_teacher and not preview
    flags = await features.load(session)
    return templates.TemplateResponse(
        request,
        "ai.html",
        {
            "user": user,
            "endpoint": endpoint(),
            "teacher_page": teacher_page,
            "preview": preview,
            "tokens": [] if preview else await api_tokens.for_user(session, user),
            "max_tokens": api_tokens.MAX_PER_USER,
            "issued": issued,
            "snippets": snippets(issued or PLACEHOLDER),
            # Все инструменты роли, включая те, что требуют ключа на запись:
            # человек должен видеть, ради чего такой ключ выпускать.
            "tools": registry.for_role(
                Role.teacher if teacher_page else Role.student,
                frozenset(
                    section.key
                    for section in features.SECTIONS
                    if features.allows_role(flags, section.key, teacher=teacher_page)
                ),
            ),
            "ok": request.query_params.get("ok"),
            "error": request.query_params.get("err"),
        },
    )


# Только что выпущенный ключ доезжает до страницы в куке, а не в ответе на POST
# и не в адресе. Ответ на POST при обновлении выпустил бы второй ключ, а
# в адресе ключ остался бы в истории браузера и журналах прокси. Кука живёт
# минуту, видна только странице раздела и стирается при первом показе.
FRESH_COOKIE = "ai_fresh_key"
FRESH_SECONDS = 60


def _fresh() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt="ai-fresh-key")


def _take_fresh(request: Request) -> str | None:
    sealed = request.cookies.get(FRESH_COOKIE)
    if not sealed:
        return None
    try:
        return _fresh().loads(sealed, max_age=FRESH_SECONDS)
    except (BadSignature, SignatureExpired):
        return None


@router.get("/ai")
async def ai_page(request: Request, session: SessionDep, user: CurrentUser):
    response = await _page(request, session, user, _take_fresh(request))
    if FRESH_COOKIE in request.cookies:
        response.delete_cookie(FRESH_COOKIE, path="/ai")
    return response


@router.post("/ai/tokens")
async def issue_token(
    session: SessionDep, user: CurrentUser, name: str = Form(""), can_write: bool = Form(False)
):
    issued = await api_tokens.issue(session, user, name, can_write=can_write)
    if issued is None:
        error = _("Ключей уже %(count)s — отзови ненужный") % {"count": api_tokens.MAX_PER_USER}
        return RedirectResponse("/ai?err=" + quote(error), status_code=303)
    response = RedirectResponse("/ai", status_code=303)
    response.set_cookie(
        FRESH_COOKIE,
        _fresh().dumps(issued[1]),
        max_age=FRESH_SECONDS,
        path="/ai",
        httponly=True,
        samesite="strict",
        secure=settings.base_url.startswith("https://"),
    )
    return response


@router.post("/ai/tokens/{token_id}/revoke")
async def revoke_token(session: SessionDep, user: CurrentUser, token_id: int):
    await api_tokens.revoke(session, user, token_id)
    return RedirectResponse("/ai?ok=" + quote(_("Ключ отозван")), status_code=303)
