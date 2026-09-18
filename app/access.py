"""Кто считается студентом портала.

Telegram — способ связи, а не удостоверение: завести бота может кто угодно.
Студенчество подтверждает только учётная запись организации через OIDC.
Без подтверждения человек может войти и осмотреться, но не вступить в группу
и не попасть в общее задание.

Пока провайдер не настроен (пустой OIDC_ISSUER), проверять нечем — портал
работает как раньше, иначе первый же студент упёрся бы в закрытую дверь.
"""

from __future__ import annotations

from sqlalchemy import ColumnElement, true

from app.config import settings
from app.models import User


def gate_enabled() -> bool:
    return settings.oidc_enabled


def is_confirmed(user: User) -> bool:
    """Подтверждён ли студент. Преподаватель назначается вручную и проверки не требует."""
    if not gate_enabled():
        return True
    return user.oidc_sub is not None or user.is_teacher


def confirmed_clause() -> ColumnElement[bool]:
    """То же правило для запросов: кого считать студентом портала."""
    if not gate_enabled():
        return true()
    return User.oidc_sub.is_not(None)
