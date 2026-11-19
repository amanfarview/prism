import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch, tmp_path):
    """Tests ignore the server's .env: env secrets, dev auth, temp secrets file."""
    monkeypatch.setenv("PRISM_SECRET_BACKEND", "env")
    monkeypatch.setenv("PRISM_AUTH_MODE", "dev")
    monkeypatch.setenv("PRISM_PUBLIC_URL", "http://localhost:8040/prism")
    monkeypatch.setenv("PRISM_SESSION_SECRET", "test-session-secret-" + "x" * 40)
    monkeypatch.setenv("PRISM_ENVIRONMENT", "dev")
    monkeypatch.setenv("PRISM_BLOCK_PRIVATE_FETCH", "false")  # tests mock HTTP; tests/test_security.py covers the guard
    monkeypatch.setenv("PRISM_SECRETS_FILE", str(tmp_path / "secrets.json"))
    monkeypatch.delenv("PRISM_ENTRA_CLIENT_SECRET", raising=False)
    from prism.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/t.db"
    monkeypatch.setenv("PRISM_DATABASE_URL", url)
    from prism.config import get_settings
    get_settings.cache_clear()
    from prism.db import init_db, reset_engine, session_scope
    reset_engine(url)
    init_db()
    from prism.catalogue import ensure_defaults
    from prism.seed import seed_portcos
    with session_scope() as s:
        ensure_defaults(s)
        seed_portcos(s)
    yield session_scope
    get_settings.cache_clear()
