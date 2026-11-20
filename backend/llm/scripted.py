"""A scripted provider for end-to-end tests and offline demos.

Enabled only by configuration (LLM_PROVIDER=scripted and LLM_SCRIPT=<file>);
it answers each call with the next object from a JSON list that fits the
call: the first remaining answer that has every field the call's schema
requires. So a strategy compile, an explanation, a scan and a research plan
each take their own answers in order, whatever order the calls arrive in, and
one end-to-end spec can run alone. It is never the default and the UI shows
its name, so it cannot be mistaken for a real model.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from backend.llm.base import LLMError, LLMResponse, Usage

_instances: dict[Path, ScriptedLLM] = {}
_lock = threading.Lock()


class ScriptedLLM:
    provider = "scripted"

    def __init__(self, path: Path) -> None:
        self.model = f"scripted:{path.name}"
        self._answers: list[dict[str, Any]] = json.loads(path.read_text())
        self._lock = threading.Lock()

    def complete_json(self, *, system: str, messages: list[dict[str, Any]], schema: dict[str, Any],
                      max_tokens: int = 32000) -> LLMResponse:
        with self._lock:
            required = set(schema.get("required") or [])
            index = next((i for i, a in enumerate(self._answers) if required <= set(a)), None)
            if index is None:
                raise LLMError("The scripted model has no answer left for this kind of call.")
            answer = self._answers.pop(index)
        return LLMResponse(data=answer, model=self.model, usage=Usage(1000, 200))


def scripted_client(path: Path) -> ScriptedLLM:
    """One instance per script file, so answers are consumed across calls."""
    path = path.resolve()
    with _lock:
        if path not in _instances:
            _instances[path] = ScriptedLLM(path)
        return _instances[path]
