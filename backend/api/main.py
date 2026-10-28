"""FastAPI application — the frontend's only door into the engine.

Design rule: this layer contains no trading logic. It validates input, calls the
same functions the CLI calls, and shapes the result for the UI. If a calculation
lives here, it can diverge from what a backtest or a live session does, and the
one-engine guarantee is gone.

Endpoints live in `backend/api/routers/`, one module per area; every router that
touches per-user state takes the current user from `backend/api/deps.py`.

Run (local only; requests with a non-local Host or Origin are rejected):
    .venv/bin/uvicorn backend.api.main:app --reload --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend import __version__
from backend.api.security import LocalOnlyMiddleware
from backend.app.library import LibraryError, NotFound, ReadOnly
from backend.core.config import settings
from backend.core.log_setup import configure_logging

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    configure_logging()
    from backend.app.jobs import get_runner, shutdown_runner
    from backend.live.manager import manager

    abandoned = get_runner().recover_interrupted()
    if abandoned:
        log.warning("%d background job(s) were interrupted by the last shutdown", abandoned)

    from backend.app.app_session import get_app_sessions

    app_sessions = get_app_sessions()
    app_sessions.begin()
    pending = app_sessions.pending()
    if pending:
        log.warning("%d earlier app session(s) left downloaded data waiting for keep or "
                    "discard", len(pending))
    idle_stop = threading.Event()
    idle_thread = threading.Thread(target=_watch_idle, args=(idle_stop,), name="idle-watch",
                                   daemon=True)
    idle_thread.start()

    interrupted = manager.interrupted()
    if interrupted:
        # Never resumed automatically: the user may have closed the position by
        # hand, or may not want the session back at all.
        log.warning(
            "%d paper session(s) were interrupted today and can be resumed from the "
            "Live tab: %s",
            len(interrupted), ", ".join(s["session_id"] for s in interrupted),
        )
    log.info("Trade Terminal API started (data dir %s)", settings.data_dir)
    yield
    log.info("Shutting down: stopping running sessions and jobs")
    idle_stop.set()
    manager.stop_all("shutdown")
    shutdown_runner()
    # Already ended if the trader quit from the UI; otherwise apply the preference
    # (or leave the data pending for the next start).
    app_sessions.end("shutdown")


# How often the idle rule is checked.
IDLE_CHECK_S = 60.0


def _watch_idle(stop: threading.Event) -> None:
    """End the app session after `idle_minutes` with no UI and no paper session."""
    from backend.app.app_session import get_app_sessions
    from backend.live.manager import manager

    while not stop.wait(IDLE_CHECK_S):
        try:
            running = any(s["running"] for s in manager.active())
            get_app_sessions().check_idle(paper_running=running)
        except Exception:
            log.exception("Idle check failed")


app = FastAPI(
    lifespan=lifespan,
    title="Trade Terminal",
    description="Visual strategy definition, backtesting, paper trading and live execution.",
    version=__version__,
)

# The Vite dev server runs on a different port; in production the frontend is
# served as static files from this same origin and CORS becomes irrelevant.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Added last so it runs first: nothing, not even CORS, sees a foreign request.
app.add_middleware(LocalOnlyMiddleware)


# One error model: domain errors become {"detail": message} with a fitting status.
@app.exception_handler(LibraryError)
async def _library_error(_request: Request, exc: LibraryError) -> JSONResponse:
    status = 404 if isinstance(exc, NotFound) else 409 if isinstance(exc, ReadOnly) else 400
    return JSONResponse({"detail": str(exc)}, status_code=status)


from backend.api.routers import app as app_router  # noqa: E402
from backend.api.routers import backtest, data, jobs, live, reference, strategies  # noqa: E402

for _router in (reference.router, data.router, strategies.router, backtest.router,
                jobs.router, live.router, app_router.router):
    app.include_router(_router)
