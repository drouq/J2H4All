import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from . import api, auth, telegram
from .auth import current_user
from .calendar import routes as calendar_routes
from .config import get_settings
from .db import engine

logging.basicConfig(level=logging.INFO)

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if settings.is_production:
        settings.validate_production()
    yield


app = FastAPI(title="J2H4All — Journey to Hundred, for All", lifespan=lifespan)

_settings = get_settings()
app.add_middleware(
    SessionMiddleware,
    secret_key=_settings.secret_key,
    https_only=_settings.is_production,
    same_site="lax",
    max_age=60 * 60 * 24 * 30,
)

app.include_router(auth.router)
app.include_router(api.router)
app.include_router(telegram.router)
app.include_router(calendar_routes.auth_router)
app.include_router(calendar_routes.api_router)


@app.get("/healthz")
def healthz():
    """Unauthed liveness probe for Render — process only, deliberately NO database.

    It used to run `SELECT 1` per call. Render polls this endpoint continuously, so
    every poll reset Neon's 5-minute scale-to-zero timer and the compute never
    suspended — ~4.5 CU-hrs/day against the 100 CU-hrs/month free budget (≈18h/day
    awake for a workload that needs minutes). It was also the wrong semantic: a DB
    blip or a scale-to-zero cold start would 503 this probe and get the web service
    restarted. Liveness = "the process is up". Use /healthz/db to check the DB.
    """
    return {"status": "ok"}


@app.get("/healthz/db")
def healthz_db(user: str = Depends(current_user)):
    """Explicit DB reachability check. NOT wired to Render's healthCheckPath — this
    WAKES the Neon compute (and holds it ~5 min, the free-tier autosuspend floor), so
    call it manually when diagnosing, never on a timer.

    AUTHED, unlike /healthz: "call it manually" was a convention, not a control. The
    app sits on a public URL, so any crawler or scanner that walked to /healthz/db
    would wake the compute for ≥5 minutes a hit — the exact budget leak the /healthz
    fix closed, left reachable by anyone. Sign in and hit it from the browser."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse({"status": "degraded", "db": False}, status_code=503)
    return JSONResponse({"status": "ok", "db": True})


@app.get("/{full_path:path}", include_in_schema=False)
def spa(full_path: str):
    """Serve the built SPA; unknown paths fall back to index.html (client routing)."""
    if not FRONTEND_DIST.is_dir():
        return JSONResponse(
            {"detail": "Frontend not built. Run: cd frontend && npm run build"}, status_code=503
        )
    candidate = (FRONTEND_DIST / full_path).resolve()
    if full_path and candidate.is_file() and candidate.is_relative_to(FRONTEND_DIST):
        return FileResponse(candidate)
    return FileResponse(FRONTEND_DIST / "index.html")
