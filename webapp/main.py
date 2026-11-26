from __future__ import annotations

import hmac
import logging
import os
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import desc, select

from webapp.catalog import catalog_summary, load_catalog
from webapp.db import ActivityLog, SessionLocal, WorkerJob, init_db
from webapp.worker import ALLOWED_JOBS, start_worker, stop_worker, worker_is_alive

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("hackingtool.dashboard")

ROOT = Path(__file__).resolve().parent
app = FastAPI(title="HackingTool Dashboard", version="1.0.0")
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SECRET_KEY", "development-secret-change-me"),
    same_site="lax",
    https_only=os.getenv("COOKIE_SECURE", "true").lower() == "true",
    max_age=60 * 60 * 8,
)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
templates = Jinja2Templates(directory=str(ROOT / "templates"))

_login_attempts: dict[str, deque[float]] = defaultdict(deque)
LOGIN_WINDOW_SECONDS = 300
LOGIN_MAX_ATTEMPTS = 8


@app.on_event("startup")
def startup() -> None:
    init_db()
    start_worker()
    if not os.getenv("ADMIN_PASSWORD"):
        log.warning("ADMIN_PASSWORD is not configured; admin login is disabled")
    if os.getenv("SECRET_KEY", "development-secret-change-me") == "development-secret-change-me":
        log.warning("Set a strong SECRET_KEY before public deployment")


@app.on_event("shutdown")
def shutdown() -> None:
    stop_worker()


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    return response


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _csrf(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def _valid_csrf(request: Request, submitted: str) -> bool:
    saved = request.session.get("csrf_token", "")
    return bool(saved and submitted and hmac.compare_digest(saved, submitted))


def _is_admin(request: Request) -> bool:
    return request.session.get("is_admin") is True


def _log_event(actor: str, event: str, ip: str, details: str = "") -> None:
    try:
        with SessionLocal() as db:
            db.add(ActivityLog(actor=actor[:80], event=event[:120], ip_address=ip[:64], details=details[:2000]))
            db.commit()
    except Exception:
        log.exception("Could not save activity event")


def _login_limited(ip: str) -> bool:
    now = time.monotonic()
    attempts = _login_attempts[ip]
    while attempts and now - attempts[0] > LOGIN_WINDOW_SECONDS:
        attempts.popleft()
    return len(attempts) >= LOGIN_MAX_ATTEMPTS


def _record_login_attempt(ip: str) -> None:
    _login_attempts[ip].append(time.monotonic())


def _redirect_login() -> RedirectResponse:
    return RedirectResponse(url="/admin/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    tools = load_catalog()
    categories: dict[str, int] = {}
    for tool in tools:
        categories[tool["category"]] = categories.get(tool["category"], 0) + 1
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "tools": tools,
            "categories": categories,
            "total_tools": len(tools),
            "total_categories": len(categories),
            "csrf_token": _csrf(request),
            "is_admin": _is_admin(request),
        },
    )


@app.get("/api/tools")
async def api_tools(q: str = "", category: str = ""):
    tools = load_catalog()
    needle = q.casefold().strip()
    cat = category.casefold().strip()
    if needle:
        tools = [t for t in tools if needle in " ".join([
            t["title"], t["description"], t["category"], " ".join(t["tags"])
        ]).casefold()]
    if cat:
        tools = [t for t in tools if t["category"].casefold() == cat]
    return {"count": len(tools), "tools": tools}


@app.get("/api/status")
async def api_status():
    summary = catalog_summary()
    return {
        "status": "online",
        "worker": "online" if worker_is_alive() else "offline",
        "catalog_tools": summary["total_tools"],
        "categories": summary["total_categories"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/admin/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    if _is_admin(request):
        return RedirectResponse("/admin", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"csrf_token": _csrf(request), "error": error},
    )


@app.post("/admin/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
):
    ip = _ip(request)
    if not _valid_csrf(request, csrf_token):
        return RedirectResponse("/admin/login?error=Session+expired", status_code=303)
    if _login_limited(ip):
        _log_event("anonymous", "login_rate_limited", ip)
        return RedirectResponse("/admin/login?error=Too+many+attempts.+Try+again+later", status_code=303)

    expected_user = os.getenv("ADMIN_USERNAME", "admin")
    expected_password = os.getenv("ADMIN_PASSWORD", "")
    valid = bool(expected_password) and hmac.compare_digest(username, expected_user) and hmac.compare_digest(password, expected_password)
    if not valid:
        _record_login_attempt(ip)
        _log_event("anonymous", "login_failed", ip)
        return RedirectResponse("/admin/login?error=Invalid+credentials", status_code=303)

    request.session.clear()
    request.session["is_admin"] = True
    request.session["csrf_token"] = secrets.token_urlsafe(32)
    _log_event(expected_user, "login_success", ip)
    return RedirectResponse("/admin", status_code=303)


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    if not _is_admin(request):
        return _redirect_login()
    with SessionLocal() as db:
        logs = db.scalars(select(ActivityLog).order_by(desc(ActivityLog.id)).limit(50)).all()
        jobs = db.scalars(select(WorkerJob).order_by(desc(WorkerJob.id)).limit(30)).all()
    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={
            "csrf_token": _csrf(request),
            "logs": logs,
            "jobs": jobs,
            "worker_online": worker_is_alive(),
            "allowed_jobs": sorted(ALLOWED_JOBS),
            "is_admin": True,
        },
    )


@app.post("/admin/jobs")
async def create_job(
    request: Request,
    job_type: str = Form(...),
    csrf_token: str = Form(...),
):
    if not _is_admin(request):
        return _redirect_login()
    if not _valid_csrf(request, csrf_token):
        return RedirectResponse("/admin?error=Session+expired", status_code=303)
    if job_type not in ALLOWED_JOBS:
        return RedirectResponse("/admin?error=Unsupported+job", status_code=303)
    with SessionLocal() as db:
        job = WorkerJob(job_type=job_type, status="pending", requested_by=os.getenv("ADMIN_USERNAME", "admin"))
        db.add(job)
        db.commit()
        job_id = job.id
    _log_event(os.getenv("ADMIN_USERNAME", "admin"), "worker_job_queued", _ip(request), f"job_id={job_id}; type={job_type}")
    return RedirectResponse("/admin?queued=1", status_code=303)


@app.post("/admin/logout")
async def logout(request: Request, csrf_token: str = Form(...)):
    if _valid_csrf(request, csrf_token):
        _log_event(os.getenv("ADMIN_USERNAME", "admin"), "logout", _ip(request))
        request.session.clear()
    return RedirectResponse("/", status_code=303)


@app.get("/api/logs")
async def api_logs(request: Request):
    if not _is_admin(request):
        return JSONResponse({"error": "admin authentication required"}, status_code=401)
    with SessionLocal() as db:
        logs = db.scalars(select(ActivityLog).order_by(desc(ActivityLog.id)).limit(100)).all()
        return {"logs": [
            {"id": x.id, "created_at": x.created_at.isoformat(), "actor": x.actor,
             "event": x.event, "ip_address": x.ip_address, "details": x.details}
            for x in logs
        ]}
