"""MCP по HTTP: один адрес `/mcp`, JSON-RPC в теле POST, ответ — один JSON.

Своя реализация, а не пакет `mcp`: инструментам нужны только запрос и ответ,
без потоков и сессий, а это сотня строк поверх FastAPI. Пакет притянул бы
свой веб-сервер, свою авторизацию и свой цикл жизни приложения ради того,
чем мы не пользуемся.

Разговаривает с клиентами двух поколений протокола:

- 2025-03-26 … 2025-11-25 — клиент начинает с `initialize`. Сессий не заводим:
  они по спецификации необязательны, а состояния между вызовами у нас нет.
- 2026-07-28 и новее — рукопожатия нет, версия и возможности клиента приходят
  в `_meta` каждого запроса, а заголовки `Mcp-Method` и `Mcp-Name` обязаны
  совпадать с телом.

Вход — по ключу из заголовка `Authorization: Bearer …`, выпущенному на
странице «ИИ». Куки сайта здесь не принимаются: иначе любая открытая
вкладка с порталом стала бы пропуском для чужого скрипта.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app.config import settings
from app.deps import SessionDep
from app.mcp import registry, tools
from app.services import api_tokens

logger = logging.getLogger(__name__)

router = APIRouter(tags=["mcp"])

MODERN_VERSIONS = ("2026-07-28",)
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
SUPPORTED_VERSIONS = MODERN_VERSIONS + LEGACY_VERSIONS

META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_SERVER = "io.modelcontextprotocol/serverInfo"

SERVER_INFO = {"name": "sport-portal", "title": settings.app_name, "version": "1.0.0"}
CAPABILITIES = {"tools": {"listChanged": False}}

# Список инструментов зависит от роли и ключа — кэш у клиента частный, на минуту.
LIST_TTL_MS = 60_000

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
# Свои коды — из диапазона, оставленного реализациям (-32000…-32019).
UNAUTHORIZED = -32001
FORBIDDEN = -32003
# Коды спецификации 2026-07-28.
HEADER_MISMATCH = -32020
UNSUPPORTED_VERSION = -32022


class RpcError(Exception):
    def __init__(self, code: int, message: str, status: int = 200, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.data = data


def _error(request_id: Any, exc: RpcError) -> JSONResponse:
    body: dict = {"code": exc.code, "message": exc.message}
    if exc.data is not None:
        body["data"] = exc.data
    headers = {}
    if exc.status == 401:
        # Так клиент понимает, что дело в ключе, а не в сервере.
        headers["WWW-Authenticate"] = 'Bearer realm="portal"'
    return JSONResponse(
        {"jsonrpc": "2.0", "id": request_id, "error": body},
        status_code=exc.status,
        headers=headers,
    )


def _origin_allowed(request: Request) -> bool:
    """Защита от DNS rebinding: браузерный скрипт с чужого сайта не должен
    достучаться до портала. Клиенты MCP Origin не присылают вовсе."""
    origin = request.headers.get("origin")
    if not origin:
        return True
    own = urlsplit(settings.base_url)
    theirs = urlsplit(origin)
    return (theirs.scheme, theirs.netloc) == (own.scheme, own.netloc)


def _header_value(raw: str) -> str:
    """Значение `Mcp-Name`: не-ASCII клиент кодирует как `=?base64?…?=`."""
    if raw.startswith("=?base64?") and raw.endswith("?="):
        try:
            return base64.b64decode(raw[9:-2], validate=True).decode()
        except (binascii.Error, UnicodeDecodeError):
            raise RpcError(HEADER_MISMATCH, "Mcp-Name: неверный base64", 400) from None
    return raw


def _check_modern_headers(request: Request, version: str, method: str, params: dict) -> None:
    """В 2026-07-28 заголовки дублируют тело, и расхождение — отказ.

    По заголовкам маршрутизируют прокси; если они говорят одно, а тело
    другое, проверка на прокси и исполнение здесь видели бы разные запросы.
    """
    expected = {"MCP-Protocol-Version": version, "Mcp-Method": method}
    if method == "tools/call":
        expected["Mcp-Name"] = str(params.get("name", ""))
    for header, value in expected.items():
        got = request.headers.get(header)
        if got is None:
            raise RpcError(HEADER_MISMATCH, f"Нет заголовка {header}", 400)
        if header == "Mcp-Name":
            got = _header_value(got)
        if got != value:
            raise RpcError(
                HEADER_MISMATCH, f"{header}: {got!r} не совпадает с телом ({value!r})", 400
            )


async def _context(request: Request, session) -> registry.Context:
    auth = request.headers.get("authorization", "")
    scheme, _, raw = auth.partition(" ")
    if scheme.lower() != "bearer" or not raw:
        raise RpcError(
            UNAUTHORIZED, "Нужен ключ: заголовок Authorization: Bearer <ключ со страницы «ИИ»>", 401
        )
    found = await api_tokens.authenticate(session, raw)
    if found is None:
        raise RpcError(UNAUTHORIZED, "Ключ не подходит: его отозвали или он неполный", 401)
    token, user = found
    ctx = await registry.context_for(session, user, can_write=token.can_write)
    if not registry.section_open(ctx):
        raise RpcError(FORBIDDEN, "Раздел «ИИ» сейчас закрыт преподавателем", 403)
    return ctx


def _tool_result(data: dict) -> dict:
    """Ответ инструмента: тот же словарь текстом для моделей и структурой
    для клиентов, которые умеют её читать."""
    text = json.dumps(data, ensure_ascii=False, indent=1)
    return {"content": [{"type": "text", "text": text}], "structuredContent": data}


async def _call_tool(ctx: registry.Context, params: dict) -> dict:
    name = params.get("name")
    if not isinstance(name, str):
        raise RpcError(INVALID_PARAMS, "tools/call: нет имени инструмента")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise RpcError(INVALID_PARAMS, "tools/call: arguments должен быть объектом")
    try:
        return _tool_result(await registry.call(ctx, name, arguments))
    except registry.ToolError as exc:
        await ctx.session.rollback()
        return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    except Exception:
        # Модели — короткий отказ, подробности — в журнал: трассировка
        # ничего не скажет нейросети, а нам скажет всё.
        logger.exception("MCP: инструмент %s упал", name)
        await ctx.session.rollback()
        return {
            "content": [{"type": "text", "text": "Внутренняя ошибка портала, попробуй позже"}],
            "isError": True,
        }


def _negotiate_legacy(requested: Any) -> str:
    return requested if requested in LEGACY_VERSIONS else LEGACY_VERSIONS[0]


@router.post("/mcp")
async def mcp_endpoint(request: Request, session: SessionDep) -> Response:
    if not _origin_allowed(request):
        return _error(None, RpcError(FORBIDDEN, "Чужой Origin", 403))

    try:
        message = json.loads(await request.body())
    except (ValueError, UnicodeDecodeError):
        return _error(None, RpcError(PARSE_ERROR, "Тело — не JSON", 400))
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        # Пачки сообщений убраны из протокола в 2025-06-18; принимаем по одному.
        return _error(None, RpcError(INVALID_REQUEST, "Ждём один объект JSON-RPC 2.0", 400))

    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}
    if not isinstance(method, str) or not isinstance(params, dict):
        return _error(request_id, RpcError(INVALID_REQUEST, "Нет method или params", 400))

    try:
        ctx = await _context(request, session)
    except RpcError as exc:
        return _error(request_id, exc)

    # Уведомление — без id и без ответа. Нам от клиента ничего не нужно
    # знать: ни «инициализирован», ни «отменил» — состояния мы не держим.
    if "id" not in message:
        return Response(status_code=202)

    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    modern = META_VERSION in meta
    try:
        if modern:
            version = meta[META_VERSION]
            if version not in MODERN_VERSIONS:
                raise RpcError(
                    UNSUPPORTED_VERSION,
                    "Unsupported protocol version",
                    400,
                    {"supported": list(SUPPORTED_VERSIONS), "requested": version},
                )
            _check_modern_headers(request, version, method, params)
        else:
            header = request.headers.get("mcp-protocol-version")
            if header is not None and header not in SUPPORTED_VERSIONS:
                raise RpcError(
                    INVALID_REQUEST, f"Версия протокола {header} не поддерживается", 400
                )
        result = await _dispatch(ctx, method, params, modern)
    except RpcError as exc:
        return _error(request_id, exc)

    if modern:
        result = {"resultType": "complete", **result}
        result.setdefault("_meta", {})[META_SERVER] = SERVER_INFO
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})


async def _dispatch(ctx: registry.Context, method: str, params: dict, modern: bool) -> dict:
    if method == "initialize" and not modern:
        return {
            "protocolVersion": _negotiate_legacy(params.get("protocolVersion")),
            "capabilities": CAPABILITIES,
            "serverInfo": SERVER_INFO,
            "instructions": tools.instructions(ctx),
        }
    if method == "server/discover":
        return {
            "supportedVersions": list(SUPPORTED_VERSIONS),
            "capabilities": CAPABILITIES,
            "instructions": tools.instructions(ctx),
            "ttlMs": LIST_TTL_MS,
            "cacheScope": "private",
            "_meta": {META_SERVER: SERVER_INFO},
        }
    if method == "ping" and not modern:
        return {}
    if method == "tools/list":
        result: dict = {"tools": [item.schema() for item in registry.available(ctx)]}
        if modern:
            result.update(ttlMs=LIST_TTL_MS, cacheScope="private")
        return result
    if method == "tools/call":
        return await _call_tool(ctx, params)
    # Новое поколение различает «нет метода» и «нет сервера» по коду ответа.
    raise RpcError(METHOD_NOT_FOUND, f"Метода {method} нет", 404 if modern else 200)


@router.get("/mcp")
@router.delete("/mcp")
async def mcp_no_stream() -> Response:
    """Потока событий и сессий нет: ни уведомлений, ни состояния у нас не бывает."""
    return Response(status_code=405, headers={"Allow": "POST"})
