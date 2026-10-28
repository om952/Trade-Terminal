"""The app session: heartbeat, the quit dialog, and data left from earlier sessions.

The UI sends a heartbeat while it is open; after `idle_minutes` without one
(and no paper session running) the app session ends on its own. Quitting asks
what to do with the session's downloaded data: keep it in the local library,
or discard it, optionally remembering the choice.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.api import schemas as S
from backend.api.deps import current_context
from backend.app.app_session import get_app_sessions
from backend.app.context import AppContext

log = logging.getLogger(__name__)

router = APIRouter()

# How long after answering /api/app/quit the server stops, so the response
# reaches the browser first.
EXIT_DELAY_S = 0.5


class QuitRequest(BaseModel):
    # None applies the saved preference; "ask" in the preference with no
    # decision leaves the data pending for the next start.
    decision: Literal["keep", "discard"] | None = None
    remember: bool = False
    # Stop the server after ending the session. Off for the desktop wrapper,
    # which stops the process itself, and for tests.
    exit: bool = True


class ResolveRequest(BaseModel):
    decision: Literal["keep", "discard"]
    remember: bool = False


@router.get("/api/app/session", response_model=S.AppSessionResponse)
def app_session(ctx: AppContext = Depends(current_context)) -> dict:
    """The current app session: what it has downloaded, and the retention preference."""
    return {"session": get_app_sessions(ctx).info()}


@router.post("/api/app/heartbeat", response_model=S.AppSessionResponse)
def heartbeat(ctx: AppContext = Depends(current_context)) -> dict:
    sessions = get_app_sessions(ctx)
    sessions.heartbeat()
    return {"session": sessions.info()}


@router.post("/api/app/quit", response_model=S.QuitOut)
def quit_app(req: QuitRequest, ctx: AppContext = Depends(current_context)) -> dict:
    from backend.live.manager import manager

    running = [s["session_id"] for s in manager.active() if s["running"]]
    manager.stop_all("shutdown")  # resumable: the Live tab offers them next time
    result = get_app_sessions(ctx).end("quit", req.decision, req.remember)
    if req.exit:
        log.info("Quit requested: stopping the server")
        threading.Timer(EXIT_DELAY_S, os.kill, (os.getpid(), signal.SIGINT)).start()
    return {**result, "stopped_paper_sessions": running, "exiting": req.exit}


@router.get("/api/app/pending", response_model=S.PendingListOut)
def pending(ctx: AppContext = Depends(current_context)) -> dict:
    """Earlier sessions whose downloaded data is waiting for keep or discard."""
    return {"pending": get_app_sessions(ctx).pending()}


@router.post("/api/app/pending/{session_id}", response_model=S.ResolveOut)
def resolve_pending(session_id: str, req: ResolveRequest,
                    ctx: AppContext = Depends(current_context)) -> dict:
    try:
        return get_app_sessions(ctx).resolve(session_id, req.decision, req.remember)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
