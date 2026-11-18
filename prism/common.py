"""Small shared helpers: settings store, audit log, Teams alerts."""
from __future__ import annotations

import logging

import httpx

from .models import AppSetting, AuditLog
from .secrets import get_secret

log = logging.getLogger("prism")


def get_setting(s, key: str, default=None):
    row = s.get(AppSetting, key)
    return row.value if row else default


def set_setting(s, key: str, value) -> None:
    row = s.get(AppSetting, key)
    if row:
        row.value = value
    else:
        s.add(AppSetting(key=key, value=value))


def audit(s, user: str, action: str, **detail) -> None:
    s.add(AuditLog(user=user or "system", action=action, detail=detail))


def notify(title: str, text: str) -> None:
    """Post to the Teams channel Orbit already uses for alerts, if configured."""
    from .config import get_settings
    try:
        url = get_secret(get_settings().teams_webhook_secret_name)
    except Exception:
        url = None
    if not url:
        log.info("ALERT %s: %s", title, text)
        return
    try:
        httpx.post(url, json={"title": f"Prism: {title}", "text": text}, timeout=15)
    except Exception as e:  # alerts must never break a run
        log.warning("Teams alert failed: %s", e)
