"""The provider-neutral interface the agent codes against."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class LLMError(RuntimeError):
    """A model call failed in a way the caller should report, not retry blindly."""


class LLMUnavailable(LLMError):
    """No provider is configured (no key), or the provider cannot be reached."""


class LLMRefusal(LLMError):
    """The model declined the request. `category` comes from the provider, if any."""

    def __init__(self, message: str, category: str | None = None) -> None:
        super().__init__(message)
        self.category = category


class LLMTruncated(LLMError):
    """The answer hit the output limit and is incomplete."""


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
        )


@dataclass(frozen=True)
class LLMResponse:
    data: dict[str, Any]            # the parsed JSON answer
    model: str                      # the model that actually answered
    usage: Usage = field(default_factory=Usage)
    fell_back: bool = False         # a fallback model answered after a decline


class LLMClient(Protocol):
    provider: str
    model: str

    def complete_json(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        schema: dict[str, Any],
        max_tokens: int = 32000,
    ) -> LLMResponse:
        """One call whose answer is a JSON object matching `schema`.

        `messages` alternate user/assistant, each {"role": ..., "content": str}.
        Raises LLMRefusal, LLMTruncated, LLMUnavailable or LLMError.
        """
        ...
