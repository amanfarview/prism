"""Settings and secrets plumbing for deploy scripts. Standard library only (Key Vault export aside), never
prints a secret value.

  envtool.py set FILE KEY=VALUE ...      change/add lines, keep every other line, owner and mode (values quoted)
  envtool.py import OLD_ENV NEW_ENV      write /etc/prism/prism.env from the old /home/azureuser/prism/.env
                                         (paths moved, new session secret, secrets left out: they go to the
                                         secrets file). OLD_ENV may be missing: then a template is written.
  envtool.py export                      run in the OLD app's environment: print every Prism secret it can
                                         reach as JSON on stdout (Key Vault, data/secrets.json, env)
  envtool.py merge-secrets DEST          read that JSON on stdin, add the missing names to DEST (600);
                                         names already in DEST win. Prints only counts.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sys

LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$")
SECRET_NAMES = ["prism-openai-api-key", "prism-anthropic-api-key", "prism-gemini-api-key", "prism-perplexity-api-key",
                "prism-xai-api-key", "prism-deepseek-api-key", "prism-openai-compat-api-key", "prism-dataforseo-credentials",
                "prism-teams-webhook", "prism-entra-client-secret"]
KEEP = ["PRISM_PUBLIC_URL", "PRISM_BASE_PATH", "PRISM_ENTRA_TENANT_ID", "PRISM_ENTRA_CLIENT_ID",
        "PRISM_ENTRA_CLIENT_SECRET_NAME", "PRISM_ADMIN_EMAILS", "PRISM_ALLOWED_EMAIL_DOMAIN", "PRISM_MAX_WORKERS",
        "PRISM_REQUEST_TIMEOUT_S", "PRISM_DB_SCHEMA", "PRISM_TEAMS_WEBHOOK_SECRET_NAME"]


def parse(path: str) -> dict:
    out = {}
    if not path or not os.path.exists(path):
        return out
    for line in open(path).read().splitlines():
        m = LINE.match(line)
        if m and not line.lstrip().startswith("#"):
            v = m.group(2).strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            out[m.group(1)] = v
    return out


def _q(k: str, v: str) -> str:
    if any(c in v for c in '"\\\n'):
        sys.exit(f"{k}: value contains a quote, backslash or newline; can't write it safely. Nothing written.")
    return f'{k}="{v}"'


def write_atomic(path: str, text: str, like: str | None = None) -> None:
    st = os.stat(like or path) if os.path.exists(like or path) else None
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    if st:
        os.chown(tmp, st.st_uid, st.st_gid)
        os.chmod(tmp, st.st_mode & 0o777)
    os.replace(tmp, path)


def cmd_set(path: str, pairs: list[str]) -> None:
    updates = dict(p.partition("=")[::2] for p in pairs)
    lines, seen = [], set()
    for line in (open(path).read().splitlines() if os.path.exists(path) else []):
        m = LINE.match(line)
        if m and m.group(1) in updates and not line.lstrip().startswith("#"):
            lines.append(_q(m.group(1), updates[m.group(1)]))
            seen.add(m.group(1))
        else:
            lines.append(line)
    lines += [_q(k, v) for k, v in updates.items() if k not in seen]
    write_atomic(path, "\n".join(lines) + "\n")


def cmd_import(old_path: str, new_path: str) -> None:
    old = parse(old_path)
    url = old.get("PRISM_DATABASE_URL", "")
    public = old.get("PRISM_PUBLIC_URL", "")
    if not public or "localhost" in public or "127.0.0.1" in public:
        public = "https://jarvis.farviewequity.com/prism"
    v = {k: old[k] for k in KEEP if old.get(k)}
    v.update({
        "PRISM_ENVIRONMENT": "production",
        "PRISM_AUTH_MODE": "entra",
        "PRISM_PUBLIC_URL": public,
        "PRISM_BASE_PATH": old.get("PRISM_BASE_PATH") or "/prism",
        "PRISM_SESSION_SECRET": secrets.token_urlsafe(48),  # new: the old one sat in a file azureuser can read
        "PRISM_SECRET_BACKEND": "file",
        "PRISM_SECRETS_FILE": "/var/lib/prism/secrets.json",
        "PRISM_DATABASE_URL": url if url.startswith("mssql") else "sqlite:////var/lib/prism/prism.db",
        "PRISM_PORT": "8040",
        "PRISM_MCP_PORT": "8041",
    })
    v.setdefault("PRISM_ADMIN_EMAILS", "aman@farviewequity.com")
    v.setdefault("PRISM_ENTRA_TENANT_ID", "")
    v.setdefault("PRISM_ENTRA_CLIENT_ID", "")
    head = ["# Prism production settings. root:root 600; systemd loads it into prism-web/worker/mcp/health.",
            "# Secrets are NOT here: they live in /var/lib/prism/secrets.json (prism:prism 600).",
            "# Change: sudo nano /etc/prism/prism.env, then sudo systemctl restart prism-web prism-worker prism-mcp",
            f"# Written by deploy/install.sh from {old_path if old else 'a template'}."]
    if old.get("PRISM_KEY_VAULT_URL"):
        head.append(f"# Previously used Key Vault (no longer read): {old['PRISM_KEY_VAULT_URL']}")
    order = ["PRISM_ENVIRONMENT", "PRISM_AUTH_MODE", "PRISM_PUBLIC_URL", "PRISM_BASE_PATH", "PRISM_SESSION_SECRET",
             "PRISM_ENTRA_TENANT_ID", "PRISM_ENTRA_CLIENT_ID", "PRISM_ADMIN_EMAILS", "PRISM_DATABASE_URL",
             "PRISM_SECRET_BACKEND", "PRISM_SECRETS_FILE", "PRISM_PORT", "PRISM_MCP_PORT"]
    body = [_q(k, v[k]) for k in order] + [_q(k, v[k]) for k in v if k not in order]
    write_atomic(new_path, "\n".join(head + body) + "\n")


def cmd_export() -> None:
    """Runs as azureuser with the OLD .env exported. Prints JSON only."""
    backend = os.environ.get("PRISM_SECRET_BACKEND", "env")
    entra_name = os.environ.get("PRISM_ENTRA_CLIENT_SECRET_NAME") or "prism-entra-client-secret"
    names = sorted(set(SECRET_NAMES + [entra_name]))
    out: dict[str, str] = {}
    if backend == "keyvault" and os.environ.get("PRISM_KEY_VAULT_URL"):
        try:
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.secrets import SecretClient
            c = SecretClient(os.environ["PRISM_KEY_VAULT_URL"], DefaultAzureCredential())
            for n in names:
                try:
                    val = c.get_secret(n).value
                    if val:
                        out[n] = val
                except Exception:  # noqa: BLE001  not found / no access: skip that one
                    pass
        except Exception as e:  # noqa: BLE001
            print(f"Key Vault not readable ({type(e).__name__}); continuing with the other sources", file=sys.stderr)
    path = os.environ.get("PRISM_SECRETS_FILE", "data/secrets.json")
    if os.path.exists(path):
        try:
            for k, val in json.load(open(path)).items():
                if val:
                    out.setdefault(k, val)
        except ValueError:
            print(f"{path} isn't valid JSON; skipped", file=sys.stderr)
    for k, val in os.environ.items():
        if k.startswith("PRISM_SECRET_") and k not in ("PRISM_SECRET_BACKEND",) and val:
            out.setdefault(k[len("PRISM_SECRET_"):].lower().replace("_", "-"), val)
    if os.environ.get("PRISM_ENTRA_CLIENT_SECRET"):
        out.setdefault(entra_name, os.environ["PRISM_ENTRA_CLIENT_SECRET"])
    sys.stdout.write(json.dumps(out))


def cmd_merge(dest: str) -> None:
    raw = sys.stdin.read().strip() or "{}"
    try:
        incoming = json.loads(raw)
    except ValueError:
        sys.exit("merge-secrets: input isn't JSON; nothing written")
    current = json.load(open(dest)) if os.path.exists(dest) else {}
    added = [k for k in incoming if k not in current and incoming[k]]
    for k in added:
        current[k] = incoming[k]
    if added or not os.path.exists(dest):
        write_atomic(dest, json.dumps(current, indent=0))
        os.chmod(dest, 0o600)
    print(f"  ..  secrets file: {len(added)} added ({', '.join(sorted(added)) or 'none'}), {len(current)} in total")


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        sys.exit(__doc__)
    if a[0] == "set":
        cmd_set(a[1], a[2:])
    elif a[0] == "import":
        cmd_import(a[1], a[2])
    elif a[0] == "export":
        cmd_export()
    elif a[0] == "merge-secrets":
        cmd_merge(a[1])
    elif a[0].endswith(".env") or "/" in a[0]:  # old form: envtool.py FILE KEY=VALUE ...
        cmd_set(a[0], a[1:])
    else:
        sys.exit(__doc__)
