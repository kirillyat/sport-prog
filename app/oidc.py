"""OpenID Connect для входа через Authentik (или другой провайдер с discovery).

Authorization Code + PKCE. ID-токен приходит по защищённому бэкенд-каналу
прямо от провайдера, поэтому подпись локально не проверяем — сверяем nonce
и берём профиль с userinfo. Публичного JWKS-парсера в зависимостях нет
намеренно: меньше библиотек, меньше поверхности.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import settings
from app.i18n import translate as _

STATE_COOKIE = "sport_oidc"
STATE_TTL = 600


class OIDCError(RuntimeError):
    """Провайдер недоступен, ответ не тот или пользователь отказал."""


@dataclass(slots=True)
class Discovery:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    userinfo_endpoint: str | None


_discovery: Discovery | None = None


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(settings.http_timeout)


async def discover(force: bool = False) -> Discovery:
    global _discovery
    if _discovery is not None and not force:
        return _discovery
    url = settings.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration"
    try:
        async with httpx.AsyncClient(timeout=_timeout()) as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OIDCError(_("discovery недоступен: %(why)s") % {"why": exc}) from exc
    try:
        _discovery = Discovery(
            issuer=data["issuer"],
            authorization_endpoint=data["authorization_endpoint"],
            token_endpoint=data["token_endpoint"],
            userinfo_endpoint=data.get("userinfo_endpoint"),
        )
    except KeyError as exc:
        raise OIDCError(_("в discovery нет поля %(field)s") % {"field": exc}) from exc
    return _discovery


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt="oidc-state-v1")


def pack_state(payload: dict) -> str:
    return _serializer().dumps(payload)


def unpack_state(token: str | None) -> dict | None:
    if not token:
        return None
    try:
        return _serializer().loads(token, max_age=STATE_TTL)
    except (BadSignature, SignatureExpired):
        return None


def authorization_url(disc: Discovery, *, state: str, nonce: str, code_challenge: str) -> str:
    params = {
        "response_type": "code",
        "client_id": settings.oidc_client_id,
        "redirect_uri": settings.oidc_redirect_uri,
        "scope": settings.oidc_scopes,
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{disc.authorization_endpoint}?{urlencode(params)}"


async def exchange_code(disc: Discovery, code: str, code_verifier: str) -> dict:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.oidc_redirect_uri,
        "client_id": settings.oidc_client_id,
        "code_verifier": code_verifier,
    }
    if settings.oidc_client_secret:
        data["client_secret"] = settings.oidc_client_secret
    try:
        async with httpx.AsyncClient(timeout=_timeout()) as client:
            response = await client.post(disc.token_endpoint, data=data)
    except httpx.HTTPError as exc:
        raise OIDCError(_("token endpoint недоступен: %(why)s") % {"why": exc}) from exc
    if response.status_code >= 400:
        raise OIDCError(
            _("провайдер отказал в обмене кода (%(code)s)") % {"code": response.status_code}
        )
    try:
        tokens = response.json()
    except ValueError as exc:
        raise OIDCError(_("token endpoint вернул не JSON")) from exc
    if "id_token" not in tokens:
        raise OIDCError(_("в ответе нет id_token — проверь scope openid"))
    return tokens


def id_token_claims(id_token: str) -> dict:
    """Payload JWT без проверки подписи — только для nonce и sub (см. докстринг модуля)."""
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError) as exc:
        raise OIDCError(_("id_token не разбирается")) from exc


async def fetch_userinfo(disc: Discovery, access_token: str) -> dict:
    if not disc.userinfo_endpoint:
        return {}
    try:
        async with httpx.AsyncClient(timeout=_timeout()) as client:
            response = await client.get(
                disc.userinfo_endpoint, headers={"Authorization": f"Bearer {access_token}"}
            )
            response.raise_for_status()
            return response.json()
    except (httpx.HTTPError, ValueError):
        # Профиль — украшение; sub и группы обычно есть и в id_token.
        return {}


def display_name_from(claims: dict) -> str:
    for key in ("name", "preferred_username", "nickname", "email"):
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:120]
    return f"user-{claims.get('sub', '')[:8]}"


def groups_from(claims: dict) -> set[str]:
    raw = claims.get(settings.oidc_groups_claim)
    if isinstance(raw, str):
        raw = [raw]
    return {str(g) for g in raw} if isinstance(raw, list) else set()
