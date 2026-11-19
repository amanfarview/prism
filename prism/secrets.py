"""Secret storage.

Production on the Jarvis VM: "file" backend, /var/lib/prism/secrets.json, mode 600, owned by the prism
service user, so no other app's user can read it. Key Vault through the VM's managed identity is still
supported, but on a shared VM every process can ask the metadata endpoint for that identity's token, so
it does not isolate one app's keys from another's (see DEPLOY.md, "Why not Key Vault").
Local: environment variables named PRISM_SECRET_<NAME> (dashes become underscores).

Secrets are never written to the database or logs. The DB only keeps the secret
name and the last 4 characters so the Command Centre can show which key is set.
"""
from __future__ import annotations

import os
import threading
import time

from .config import get_settings

_cache: dict[str, tuple[float, str]] = {}
_lock = threading.Lock()
_TTL = 300  # seconds


class SecretError(RuntimeError):
    pass


def _env_name(name: str) -> str:
    return "PRISM_SECRET_" + name.upper().replace("-", "_")


def _kv_client():
    from azure.identity import DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient

    url = get_settings().key_vault_url
    if not url:
        raise SecretError("PRISM_KEY_VAULT_URL is not set")
    return SecretClient(vault_url=url, credential=DefaultAzureCredential())


def _file_read() -> dict:
    import json
    import logging
    path = get_settings().secrets_file
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return {}
    if st.st_mode & 0o077:  # group/other can read it: tighten before using it
        logging.getLogger("prism").warning("secrets file %s was mode %o; set to 600", path, st.st_mode & 0o777)
        os.chmod(path, 0o600)
    with open(path) as f:
        return json.load(f)


def _file_write(data: dict) -> None:
    import json
    path = get_settings().secrets_file
    os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def get_secret(name: str) -> str | None:
    with _lock:
        hit = _cache.get(name)
        if hit and time.time() - hit[0] < _TTL:
            return hit[1]
    backend = get_settings().secret_backend
    value: str | None
    if backend == "keyvault":
        try:
            value = _kv_client().get_secret(name).value
        except Exception as e:  # not found or no access
            if "NotFound" in type(e).__name__ or "not found" in str(e).lower():
                value = None
            else:
                raise SecretError(f"Key Vault read failed for {name}: {e}") from e
    elif backend == "file":
        value = _file_read().get(name) or os.environ.get(_env_name(name))
    else:
        value = os.environ.get(_env_name(name))
    if value:
        with _lock:
            _cache[name] = (time.time(), value)
    return value


def set_secret(name: str, value: str) -> None:
    backend = get_settings().secret_backend
    if backend == "keyvault":
        _kv_client().set_secret(name, value, content_type="prism-api-key")
    elif backend == "file":
        data = _file_read()
        data[name] = value
        _file_write(data)
    else:
        os.environ[_env_name(name)] = value  # dev only: lives for this process
    with _lock:
        _cache[name] = (time.time(), value)


def forget(name: str) -> None:
    with _lock:
        _cache.pop(name, None)
