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


def teams_card(title: str, lines: list[str], ok: bool = False) -> dict:
    """An Adaptive Card message. Farview's Teams webhooks are Power Automate flows: they accept a plain
    {"text": ...} payload with HTTP 202 and then never show it, so alerts must be cards."""
    card = {"type": "AdaptiveCard", "$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "version": "1.4",
            "body": [{"type": "TextBlock", "text": title, "weight": "Bolder", "wrap": True,
                      "color": "Good" if ok else "Attention"}]
            + [{"type": "TextBlock", "text": t, "wrap": True, "spacing": "Small"} for t in lines if t]}
    return {"type": "message", "attachments": [
        {"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None, "content": card}]}


def notify(title: str, text: str) -> None:
    """Post an alert to Teams (secret prism-teams-webhook), or to the log if none is set. Staging never posts."""
    from .config import get_settings
    st = get_settings()
    url = None
    if not st.is_staging:
        try:
            url = get_secret(st.teams_webhook_secret_name)
        except Exception:
            url = None
    if not url:
        log.info("ALERT %s: %s", title, text)
        return
    try:
        httpx.post(url, json=teams_card(f"Prism: {title}", [text]), timeout=15)
    except Exception as e:  # alerts must never break a run
        log.warning("Teams alert failed: %s", type(e).__name__)
