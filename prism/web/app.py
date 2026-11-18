"""FastAPI app: dashboards, Command Centre, REST API. Served under PRISM_BASE_PATH."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from ..catalogue import ensure_defaults
from ..config import get_settings
from ..db import SessionLocal, init_db, session_scope
from ..models import Brand
from . import auth

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))


# ---------------------------------------------------------------- template helpers
def pct(x, digits=0):
    if x is None:
        return "–"
    return f"{x * 100:.{digits}f}%"


def ci(t, digits=0):
    if not t:
        return ""
    lo, hi = t
    return f"{lo * 100:.{digits}f}-{hi * 100:.{digits}f}%"


def money(x):
    return "–" if x is None else (f"${x:,.2f}" if x < 1000 else f"${x:,.0f}")


def cost(x):
    return "–" if x is None else (f"${x:.4f}" if x < 1 else f"${x:,.2f}")


def dt(d, fmt="%d %b %Y %H:%M"):
    return d.strftime(fmt) if d else "–"


def num(x, digits=1):
    return "–" if x is None else f"{x:.{digits}f}"


templates.env.filters.update(pct=pct, ci=ci, money=money, dt=dt, num=num, cost=cost)


def get_db():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


class LoginRequired(Exception):
    pass


class AdminRequired(Exception):
    pass


def require_user(request: Request) -> auth.User:
    u = auth.current_user(request)
    if not u:
        raise LoginRequired()
    return u


def require_admin(request: Request) -> auth.User:
    u = require_user(request)
    if not u.is_admin:
        raise AdminRequired()
    return u


def render(request: Request, name: str, s, user, **ctx) -> HTMLResponse:
    st = get_settings()
    brands = list(s.scalars(select(Brand).where(Brand.active.is_(True)).order_by(Brand.name)))
    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, name, {
        "user": user, "base": st.base_path, "brands_nav": brands, "csrf": auth.csrf_token(request),
        "flash": flash, **ctx})


def flash(request: Request, msg: str, kind: str = "ok") -> None:
    request.session["flash"] = {"msg": msg, "kind": kind}


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(get_settings().base_path + path, status_code=303)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    init_db()
    with session_scope() as s:
        ensure_defaults(s)
    yield


def create_app() -> FastAPI:
    st = get_settings()
    app = FastAPI(title="Prism", docs_url=None, redoc_url=None, openapi_url=None,  # no public API docs
                  lifespan=_lifespan)
    app.add_middleware(SessionMiddleware, secret_key=st.session_secret, https_only=st.auth_mode == "entra",
                       same_site="lax", session_cookie="prism_session", max_age=12 * 3600)
    app.mount(st.base_path + "/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @app.exception_handler(LoginRequired)
    async def _login(request: Request, exc):  # noqa: ANN001
        if request.url.path.startswith(st.base_path + "/api/"):
            return JSONResponse({"error": "unauthorised"}, status_code=401)
        return RedirectResponse(st.base_path + "/login", status_code=303)

    @app.exception_handler(AdminRequired)
    async def _admin(request: Request, exc):  # noqa: ANN001
        return HTMLResponse("<h3>Command Centre is for Prism admins.</h3>", status_code=403)

    root = APIRouter(prefix=st.base_path)

    @root.get("/healthz")
    def healthz():
        return {"ok": True}  # deliberately says nothing else

    @root.get("/login")
    def login(request: Request):
        if st.auth_mode == "dev":
            return redirect("/")
        return RedirectResponse(auth.start_login(request), status_code=303)

    @root.get("/auth/callback")
    def callback(request: Request):
        auth.finish_login(request)
        return redirect("/")

    @root.get("/logout")
    def logout(request: Request):
        request.session.clear()
        return redirect("/login") if st.auth_mode != "dev" else redirect("/")

    app.include_router(root)
    from . import api, command, pages
    app.include_router(pages.router, prefix=st.base_path)
    app.include_router(command.router, prefix=st.base_path + "/cc")
    app.include_router(api.router, prefix=st.base_path + "/api/v1")

    @app.get("/")
    def _root():
        return RedirectResponse(st.base_path + "/", status_code=303)

    return app


def mark(html: str) -> Markup:
    return Markup(html)


__all__ = ["create_app", "get_db", "require_user", "require_admin", "render", "flash", "redirect", "Depends"]
