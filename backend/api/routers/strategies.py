"""Strategy documents: validation, and the library of saved strategies with
their version history (backend.app.library)."""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend.api.common import _format_validation_error
from backend.api import schemas as S
from backend.api.deps import current_context
from backend.app.context import AppContext
from backend.app.library import StrategyLibrary
from backend.app.runs import RunStore
from backend.strategy.schema import Strategy

log = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/strategies/validate")
def validate_strategy(document: dict[str, Any]) -> dict:
    """Validate a strategy document and return its hash.

    The builder calls this on every edit, so a typo in an indicator reference
    surfaces immediately rather than as an empty trade log later.
    """
    try:
        strategy = Strategy.model_validate(document)
    except Exception as exc:
        return {"valid": False, "errors": _format_validation_error(exc)}

    return {
        "valid": True,
        "id": strategy.id,
        "hash": strategy.content_hash(),
        "summary": strategy.summary(),
        "is_intraday": strategy.is_intraday,
        "has_options": strategy.has_options,
        "document": strategy.model_dump(mode="json"),
    }


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------

_templates_imported: set[tuple[str, str]] = set()


def _library(ctx: AppContext) -> StrategyLibrary:
    """The user's library, with the shipped templates imported once per process."""
    library = StrategyLibrary(ctx)
    key = (ctx.user_id, str(ctx.data_dir))
    if key not in _templates_imported:
        library.import_templates()
        _templates_imported.add(key)
    return library


def _parse(document: dict[str, Any]) -> Strategy:
    try:
        return Strategy.model_validate(document)
    except Exception as exc:
        raise HTTPException(422, {"errors": _format_validation_error(exc)}) from exc


class SaveRequest(BaseModel):
    document: dict[str, Any]
    name: str | None = Field(default=None, max_length=120)
    source: Literal["builder", "llm", "import"] = "builder"
    note: str | None = Field(default=None, max_length=500)


class VersionRequest(BaseModel):
    document: dict[str, Any]
    source: Literal["builder", "llm", "import"] = "builder"
    note: str | None = Field(default=None, max_length=500)


class ForkRequest(BaseModel):
    name: str | None = Field(default=None, max_length=120)


class UpdateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    archived: bool | None = None


@router.get("/api/strategies", response_model=S.StrategyListOut)
def list_strategies(
    include_archived: bool = False,
    include_templates: bool = True,
    ctx: AppContext = Depends(current_context),
) -> dict:
    library = _library(ctx)
    return {"strategies": [s.to_dict() for s in library.list_strategies(
        include_archived=include_archived, include_templates=include_templates)]}


@router.post("/api/strategies", status_code=201, response_model=S.StrategyOneOut)
def create_strategy(req: SaveRequest, ctx: AppContext = Depends(current_context)) -> dict:
    strategy = _parse(req.document)
    record = _library(ctx).create(strategy, req.source, name=req.name, note=req.note)
    return {"strategy": record.to_dict()}


@router.get("/api/strategies/{strategy_id}", response_model=S.StrategyDetailOut)
def get_strategy(strategy_id: str, ctx: AppContext = Depends(current_context)) -> dict:
    library = _library(ctx)
    record = library.get(strategy_id)
    return {
        "strategy": record.to_dict(),
        "head": library.version(strategy_id).to_dict(),
        "trials": RunStore(ctx).trials(record.family_id),
    }


@router.get("/api/strategies/{strategy_id}/versions", response_model=S.VersionListOut)
def list_versions(strategy_id: str, ctx: AppContext = Depends(current_context)) -> dict:
    versions = _library(ctx).versions(strategy_id)
    return {"versions": [v.to_dict(include_document=False) for v in versions]}


@router.get("/api/strategies/{strategy_id}/versions/{content_hash}",
            response_model=S.VersionOneOut)
def get_version(strategy_id: str, content_hash: str, ctx: AppContext = Depends(current_context)) -> dict:
    return {"version": _library(ctx).version(strategy_id, content_hash).to_dict()}


@router.post("/api/strategies/{strategy_id}/versions", status_code=201,
             response_model=S.SavedVersionOut)
def save_version(strategy_id: str, req: VersionRequest, ctx: AppContext = Depends(current_context)) -> dict:
    strategy = _parse(req.document)
    library = _library(ctx)
    version = library.save_version(strategy_id, strategy, req.source, note=req.note)
    return {"strategy": library.get(strategy_id).to_dict(), "version": version.to_dict()}


@router.post("/api/strategies/{strategy_id}/fork", status_code=201,
             response_model=S.StrategyOneOut)
def fork_strategy(strategy_id: str, req: ForkRequest, ctx: AppContext = Depends(current_context)) -> dict:
    return {"strategy": _library(ctx).fork(strategy_id, name=req.name).to_dict()}


@router.patch("/api/strategies/{strategy_id}", response_model=S.StrategyOneOut)
def update_strategy(strategy_id: str, req: UpdateRequest, ctx: AppContext = Depends(current_context)) -> dict:
    library = _library(ctx)
    if req.name is not None:
        library.rename(strategy_id, req.name)
    if req.archived is not None:
        library.set_archived(strategy_id, req.archived)
    return {"strategy": library.get(strategy_id).to_dict()}


@router.delete("/api/strategies/{strategy_id}", status_code=204)
def delete_strategy(strategy_id: str, ctx: AppContext = Depends(current_context)) -> None:
    _library(ctx).delete(strategy_id)


# ---------------------------------------------------------------------------
# Run history
# ---------------------------------------------------------------------------

@router.get("/api/runs", response_model=S.RunListOut)
def list_runs(
    library_id: str | None = None,
    strategy_hash: str | None = None,
    kind: Literal["backtest", "paper", "replay", "live"] | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    ctx: AppContext = Depends(current_context),
) -> dict:
    runs = RunStore(ctx).list_runs(
        strategy_id=library_id, strategy_hash=strategy_hash, kind=kind, limit=limit)
    return {"runs": [r.to_dict() for r in runs]}


@router.get("/api/runs/{run_id}", response_model=S.RunOneOut)
def get_run(run_id: str, ctx: AppContext = Depends(current_context)) -> dict:
    try:
        return {"run": RunStore(ctx).get(run_id).to_dict()}
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
