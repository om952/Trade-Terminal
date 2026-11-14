"""Hard caps on what one compile may spend: tokens, model calls, wall-clock.

Charged after every call and checked before the next. Exceeding a cap stops the
work with a clear error rather than letting a repair loop run up a bill. The
counters are shown to the user as the compile runs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from backend.llm.base import LLMError, Usage


class BudgetExceeded(LLMError):
    pass


@dataclass
class Budget:
    max_tokens: int
    max_steps: int
    max_seconds: float
    used: Usage = field(default_factory=Usage)
    steps: int = 0
    started: float = field(default_factory=time.monotonic)

    @classmethod
    def from_settings(cls) -> Budget:
        from backend.core.config import settings

        return cls(
            max_tokens=settings.llm_max_tokens_per_compile,
            max_steps=settings.llm_max_steps,
            max_seconds=settings.llm_max_seconds,
        )

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def check(self) -> None:
        """Raise if another model call would break a cap."""
        if self.steps >= self.max_steps:
            raise BudgetExceeded(
                f"Stopped after {self.steps} model calls (limit {self.max_steps})."
            )
        if self.used.total >= self.max_tokens:
            raise BudgetExceeded(
                f"Stopped after {self.used.total:,} tokens (limit {self.max_tokens:,})."
            )
        if self.elapsed >= self.max_seconds:
            raise BudgetExceeded(
                f"Stopped after {self.elapsed:.0f}s (limit {self.max_seconds:.0f}s)."
            )

    def charge(self, usage: Usage) -> None:
        self.steps += 1
        self.used = self.used + usage

    def snapshot(self) -> dict:
        return {
            "steps": self.steps, "max_steps": self.max_steps,
            "tokens": self.used.total, "max_tokens": self.max_tokens,
            "input_tokens": self.used.input_tokens, "output_tokens": self.used.output_tokens,
            "cache_read_tokens": self.used.cache_read_tokens,
            "seconds": round(self.elapsed, 1), "max_seconds": self.max_seconds,
        }
