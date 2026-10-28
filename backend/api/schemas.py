"""Response models for the API, so the OpenAPI schema (and the TypeScript types
generated from it, frontend/src/api-types.ts) describe what endpoints return.

Covered so far: the library, runs, jobs, data plans and the app session. Older
endpoints still return plain dicts; they get models as the Phase 3 UI moves
onto them.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

# -- library ------------------------------------------------------------------


class StrategyOut(BaseModel):
    id: str
    family_id: str
    name: str
    source: str
    head_hash: str
    read_only: bool
    archived: bool
    created_at: str
    updated_at: str
    version_count: int


class VersionSummaryOut(BaseModel):
    """A version without its document (for history lists)."""

    strategy_id: str
    hash: str
    parent_hash: str | None
    schema_version: int
    source: str
    model: str | None
    prompt_version: str | None
    conversation_id: str | None
    note: str | None
    created_at: str


class VersionOut(VersionSummaryOut):
    document: dict[str, Any]


class TrialsOut(BaseModel):
    backtests: int
    distinct_versions: int


class StrategyListOut(BaseModel):
    strategies: list[StrategyOut]


class StrategyOneOut(BaseModel):
    strategy: StrategyOut


class StrategyDetailOut(BaseModel):
    strategy: StrategyOut
    head: VersionOut
    trials: TrialsOut


class VersionListOut(BaseModel):
    versions: list[VersionSummaryOut]


class VersionOneOut(BaseModel):
    version: VersionOut


class SavedVersionOut(BaseModel):
    strategy: StrategyOut
    version: VersionOut


# -- runs -----------------------------------------------------------------------


class RunOut(BaseModel):
    id: str
    strategy_id: str | None
    strategy_hash: str
    family_id: str | None
    kind: str
    status: str
    params: dict[str, Any]
    data_fingerprint: dict[str, Any] | None
    metrics: dict[str, Any] | None
    result_path: str | None
    error: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None


class RunListOut(BaseModel):
    runs: list[RunOut]


class RunOneOut(BaseModel):
    run: RunOut


# -- jobs -----------------------------------------------------------------------


class JobOut(BaseModel):
    id: str
    kind: str
    status: str
    progress: float
    message: str
    eta_seconds: float | None
    params: dict[str, Any]
    result: Any = None
    error: str | None
    cancel_requested: bool
    created_at: str
    started_at: str | None
    finished_at: str | None


class JobListOut(BaseModel):
    jobs: list[JobOut]


class JobOneOut(BaseModel):
    job: JobOut


# -- data -----------------------------------------------------------------------


class DataPlanOut(BaseModel):
    instrument_key: str
    timeframe: str
    fetch_timeframe: str
    start: str
    end: str
    fetchable: bool
    complete: bool
    sessions_needed: int
    sessions_missing: int
    ranges: list[list[str]]
    requests: int
    estimated_bars: int
    estimated_bytes: int
    estimated_seconds: float
    note: str


class DataPlanResponse(BaseModel):
    plan: DataPlanOut


# -- app session ----------------------------------------------------------------


class CachedSeriesOut(BaseModel):
    instrument_key: str
    timeframe: str
    bars: int
    first_bar: str
    last_bar: str
    bytes: int


class AppSessionOut(BaseModel):
    session_id: str | None
    data_retention: str
    idle_minutes: int
    pending: int
    scratch_bytes: int = 0
    series: list[CachedSeriesOut] = []


class AppSessionResponse(BaseModel):
    session: AppSessionOut


class QuitOut(BaseModel):
    session_id: str | None
    outcome: str | None
    stopped_paper_sessions: list[str]
    exiting: bool


class PendingSessionOut(BaseModel):
    id: str
    started_at: str
    ended_at: str | None
    end_cause: str | None
    scratch_bytes: int
    series: list[CachedSeriesOut]


class PendingListOut(BaseModel):
    pending: list[PendingSessionOut]


class ResolveOut(BaseModel):
    session_id: str
    outcome: str | None
