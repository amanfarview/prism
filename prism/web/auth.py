"""Sign-in with Microsoft Entra (single-tenant, Farview's tenant only).

dev mode: no login, you are an admin (local only).
entra mode: OIDC auth-code flow via MSAL; only @farviewequity.com accounts;
admins (Command Centre) are listed in PRISM_ADMIN_EMAILS.
API/MCP callers use service tokens instead (Command Centre > API tokens).
"""
from __future__ import annotations

import hashlib
import secrets as pysecrets

from fastapi import HTTPException, Request
from sqlalchemy import select

from ..config import get_settings
from ..models import ApiToken, now
from ..secrets import get_secret


class User(dict):
    @property
    def email(self) -> str:
        return self.get("email", "")

    @property
    def is_admin(self) -> bool:
        return bool(self.get("admin"))


def _msal_app():
    import msal
    st = get_settings()
    secret = st.entra_client_secret or get_secret(st.entra_client_secret_name)
    if not (st.entra_tenant_id and st.entra_client_id and secret):
        raise HTTPException(500, "Entra sign-in is not configured (tenant, client id, client secret in Key Vault).")
    return msal.ConfidentialClientApplication(
        st.entra_client_id, authority=f"https://login.microsoftonline.com/{st.entra_tenant_id}",
        client_credential=secret)


def redirect_uri() -> str:
    return get_settings().public_url.rstrip("/") + "/auth/callback"


def start_login(request: Request) -> str:
    flow = _msal_app().initiate_auth_code_flow(["User.Read"], redirect_uri=redirect_uri())
    request.session["flow"] = flow
    return flow["auth_uri"]


def finish_login(request: Request) -> User:
    flow = request.session.pop("flow", None)
    if not flow:
        raise HTTPException(400, "Sign-in expired, please try again.")
    result = _msal_app().acquire_token_by_auth_code_flow(flow, dict(request.query_params))
    if "error" in result:
        raise HTTPException(401, result.get("error_description", "Sign-in failed"))
    claims = result.get("id_token_claims", {})
    email = (claims.get("preferred_username") or claims.get("email") or "").lower()
    st = get_settings()
    if st.entra_tenant_id and claims.get("tid") and claims["tid"].lower() != st.entra_tenant_id.lower():
        raise HTTPException(403, "This app is for Farview accounts only.")
    if not email.endswith("@" + st.allowed_email_domain):
        raise HTTPException(403, "This app is for Farview accounts only.")
    user = User(email=email, name=claims.get("name", email))
    request.session.clear()  # nothing from before sign-in survives it (new CSRF token too)
    request.session["user"] = dict(user)
    return User(user, admin=email in st.admin_list)


def current_user(request: Request) -> User | None:
    st = get_settings()
    if st.auth_mode == "dev":
        return User(email="dev@local", name="Developer", admin=True)
    u = request.session.get("user")
    if not u or not u.get("email"):
        return None
    # admin is decided on every request from PRISM_ADMIN_EMAILS, so removing someone takes effect at once
    return User(u, admin=u["email"].lower() in st.admin_list)


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = pysecrets.token_urlsafe(24)
        request.session["csrf"] = tok
    return tok


def check_csrf(request: Request, token: str | None) -> None:
    if get_settings().auth_mode == "dev" and request.session.get("csrf") is None:
        return
    expected = request.session.get("csrf")
    if not token or not expected or not pysecrets.compare_digest(str(token), str(expected)):
        raise HTTPException(400, "Form expired, please reload the page.")


# ---------------------------------------------------------------- service tokens
def hash_token(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


def new_token() -> str:
    return "prism_" + pysecrets.token_urlsafe(32)


def token_user(s, header: str | None, need_write: bool = False) -> User | None:
    if not header or not header.lower().startswith("bearer "):
        return None
    tok = header.split(" ", 1)[1].strip()
    row = s.scalar(select(ApiToken).where(ApiToken.token_hash == hash_token(tok), ApiToken.revoked.is_(False)))
    if not row or (need_write and row.scope != "write"):
        return None
    row.last_used_at = now()
    return User(email=f"token:{row.name}", name=row.name, admin=False, scope=row.scope)
