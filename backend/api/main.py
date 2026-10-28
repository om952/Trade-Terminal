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
    from backend.live.manager import manager

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
    log.info("Shutting down: stopping running sessions")
    manager.stop_all("shutdown")


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


from backend.api.routers import backtest, data, live, reference, strategies  # noqa: E402

for _router in (reference.router, data.router, strategies.router, backtest.router, live.router):
    app.include_router(_router)
