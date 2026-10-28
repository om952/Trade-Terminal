"""Strategy documents: validation."""

from __future__ import annotations

import logging
from typing import Any

from backend.strategy.schema import Strategy

from fastapi import APIRouter

from backend.api.common import (
    _format_validation_error,
)

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
