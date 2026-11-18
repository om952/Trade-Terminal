"""A scripted LLM for tests: deterministic, offline, and inspectable.

Each scripted answer is a dict (returned as the JSON answer), an exception
(raised), or a callable taking the request and returning either.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from backend.llm.base import LLMError, LLMResponse, Usage

Answer = dict[str, Any] | Exception | Callable[[dict[str, Any]], Any]


@dataclass
class FakeLLM:
    script: list[Answer] = field(default_factory=list)
    provider: str = "fake"
    model: str = "fake-model"
    usage_per_call: Usage = field(default_factory=lambda: Usage(1000, 500))
    calls: list[dict[str, Any]] = field(default_factory=list)

    def complete_json(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        schema: dict[str, Any],
        max_tokens: int = 32000,
    ) -> LLMResponse:
        request = {"system": system, "messages": [dict(m) for m in messages], "schema": schema}
        self.calls.append(request)
        if not self.script:
            raise LLMError("FakeLLM script exhausted")
        answer = self.script.pop(0)
        if callable(answer) and not isinstance(answer, Exception):
            answer = answer(request)
        if isinstance(answer, Exception):
            raise answer
        return LLMResponse(data=answer, model=self.model, usage=self.usage_per_call)
