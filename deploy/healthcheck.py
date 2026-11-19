"""Prism health check and deploy notices to Teams (secret prism-teams-webhook in /var/lib/prism/secrets.json).

  healthcheck.py check                     prism-health.timer, every 2 hours: web + database, MCP, worker
                                           heartbeat, stuck or failed runs, secrets file mode. Posts only when
                                           something is wrong, and the same problem at most every 12 hours.
  healthcheck.py deploy ok <ver> <note>    deploy/update.sh after a deploy
  healthcheck.py deploy fail <ver> <why>   deploy/update.sh when a deploy stopped

Standard library for everything except the run checks, so it still reports when the app itself is broken.
Runs as the prism user in the services' sandbox. Never prints a secret.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

WEB = "http://127.0.0.1:8040/prism/healthz/ready"
MCP = "http://127.0.0.1:8041/mcp"
STATE_DIR = os.path.dirname(os.environ.get("PRISM_SECRETS_FILE", "/var/lib/prism/secrets.json"))
STATE = os.path.join(STATE_DIR, "health-state.json")
HEARTBEAT = os.path.join(STATE_DIR, "worker.heartbeat")
REPEAT_S = 12 * 3600


def webhook() -> str:
    name = os.environ.get("PRISM_TEAMS_WEBHOOK_SECRET_NAME", "prism-teams-webhook")
    try:
        with open(os.environ.get("PRISM_SECRETS_FILE", "/var/lib/prism/secrets.json")) as f:
            return (json.load(f).get(name) or "").strip()
    except (OSError, ValueError):
        return ""


def post(title: str, lines: list[str], ok: bool = False) -> None:
    print(("OK: " if ok else "ALERT: ") + title + "".join("\n  " + t for t in lines))
    url = webhook()
    if not url:
        print("(no prism-teams-webhook set: nothing posted; add one with: sudo prism set-key teams)")
        return
    card = {"type": "AdaptiveCard", "$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "version": "1.4",
            "body": [{"type": "TextBlock", "text": title, "weight": "Bolder", "wrap": True,
                      "color": "Good" if ok else "Attention"}]
            + [{"type": "TextBlock", "text": t, "wrap": True, "spacing": "Small"} for t in lines]}
    body = json.dumps({"type": "message", "attachments": [
        {"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None, "content": card}]}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=body, method="POST",
                                                      headers={"Content-Type": "application/json"}), timeout=20).read()
    except Exception as e:  # noqa: BLE001  an alert that can't be sent must not hide the check's own result
        print(f"(couldn't post to Teams: {type(e).__name__})")


def _get(url: str, method: str = "GET", data: bytes | None = None, headers: dict | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() if e.fp else b""


def problems() -> list[str]:
    out = []
    try:
        code, body = _get(WEB)
        if code != 200 or not json.loads(body or b"{}").get("db"):
            out.append(f"Web: {WEB} answered {code}; database check failed")
    except Exception as e:  # noqa: BLE001
        out.append(f"Web didn't answer ({type(e).__name__})")
    try:
        code, _ = _get(MCP, "POST", b"{}", {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
        if code != 401:
            out.append(f"MCP answered {code} without a token (expected 401)")
    except Exception as e:  # noqa: BLE001
        out.append(f"MCP didn't answer ({type(e).__name__})")
    for unit in ("prism-web", "prism-worker", "prism-mcp"):
        st = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True).stdout.strip()
        if st != "active":
            out.append(f"{unit} is {st or 'unknown'}")
    try:
        age = time.time() - os.stat(HEARTBEAT).st_mtime
        if age > 300:
            out.append(f"Worker heartbeat is {int(age // 60)} min old (loop stuck or crashing)")
    except OSError:
        out.append("No worker heartbeat file yet")
    sf = os.environ.get("PRISM_SECRETS_FILE", "/var/lib/prism/secrets.json")
    if os.path.exists(sf) and (os.stat(sf).st_mode & 0o077):
        out.append(f"{sf} is readable by others (mode {os.stat(sf).st_mode & 0o777:o})")
    out += run_problems()
    return out


def run_problems() -> list[str]:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    try:
        from datetime import timedelta

        from sqlalchemy import select

        from prism.db import session_scope
        from prism.models import Brand, Run, now
        out = []
        with session_scope() as s:
            names = {b.id: b.name for b in s.scalars(select(Brand))}
            t = now()
            for r in s.scalars(select(Run).where(Run.status == "running", Run.started_at < t - timedelta(hours=6))):
                out.append(f"Run {r.id} ({names.get(r.brand_id, '?')}) running for over 6 hours")
            for r in s.scalars(select(Run).where(Run.status.in_(["failed", "blocked", "budget_stopped"]),
                                                 Run.created_at > t - timedelta(hours=26))):
                out.append(f"Run {r.id} ({names.get(r.brand_id, '?')}) {r.status}: {(r.note or '')[:160]}")
        return out
    except Exception as e:  # noqa: BLE001
        return [f"Couldn't read runs from the database ({type(e).__name__}: {str(e)[:120]})"]


def check() -> int:
    found = problems()
    try:
        state = json.load(open(STATE))
    except (OSError, ValueError):
        state = {}
    digest = hashlib.sha256("\n".join(sorted(found)).encode()).hexdigest()
    if not found:
        print("OK: Prism healthy")
        if state.get("digest"):
            post("Prism is healthy again", [], ok=True)
        state = {}
    elif digest != state.get("digest") or time.time() - state.get("posted", 0) > REPEAT_S:
        post("Prism needs attention", found)
        state = {"digest": digest, "posted": time.time()}
    else:
        print("ALERT (already posted): " + "; ".join(found))
    try:
        with open(STATE, "w") as f:
            json.dump(state, f)
    except OSError:
        pass
    return 1 if found else 0


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["check"]:
        sys.exit(check())
    if a[:1] == ["deploy"] and len(a) >= 3:
        ok = a[1] == "ok"
        post(f"Prism {a[2]} deployed" if ok else f"Prism deploy STOPPED at {a[2]}", [" ".join(a[3:])], ok=ok)
        sys.exit(0)
    sys.exit(__doc__)
