"""Claude, through the official Anthropic SDK.

* JSON answers use structured outputs (`output_config.format`), so the reply is
  always a parseable object of the requested shape. The strategy document
  itself travels inside it as a JSON string, because its condition groups nest
  and structured outputs do not accept recursive schemas; pydantic validates it.
* Thinking is always on for this model family; depth is set with `effort`.
* Server-side refusal fallbacks are enabled (`fallbacks: "default"`), so a
  safety-classifier decline on a benign trading request is retried on a
  suitable model inside the same call rather than failing the compile.
* Requests stream and use `get_final_message`, which keeps long, thinking-heavy
  answers clear of HTTP timeouts.
* The system prompt is large and stable (schema, indicator palette, examples),
  so it is cached: repeated compiles pay for it once per cache window.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import anthropic

from backend.llm.base import LLMError, LLMRefusal, LLMResponse, LLMTruncated, LLMUnavailable, Usage

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicClient:
    provider = "anthropic"

    def __init__(
        self,
        model: str = "claude-opus-5-5",
        effort: str = "high",
        api_key: str | None = None,
        timeout: float = 600.0,
        sdk_client: Any | None = None,
    ) -> None:
        self.model = model
        self.effort = effort
        # No key given: the SDK resolves ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN
        # or an `ant auth login` profile on its own.
        self._client = sdk_client or anthropic.Anthropic(
            api_key=api_key, timeout=timeout, max_retries=2
        )

    def complete_json(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        schema: dict[str, Any],
        max_tokens: int = 32000,
    ) -> LLMResponse:
        try:
            with self._client.beta.messages.stream(
                model=self.model,
                max_tokens=max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=messages,
                output_config={
                    "effort": self.effort,
                    "format": {"type": "json_schema", "schema": schema},
                },
                betas=[FALLBACK_BETA],
                fallbacks="default",
            ) as stream:
                message = stream.get_final_message()
        except anthropic.AuthenticationError as exc:
            raise LLMUnavailable(
                "The Anthropic API key was rejected. Set a valid key in Settings."
            ) from exc
        except anthropic.PermissionDeniedError as exc:
            raise LLMUnavailable(f"The API key may not use {self.model}: {exc.message}") from exc
        except anthropic.RateLimitError as exc:
            raise LLMError("Rate limited by Anthropic; try again in a minute.") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMUnavailable("Could not reach the Anthropic API. Check the connection.") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        except anthropic.AnthropicError as exc:
            # Raised before any request when no credential can be found.
            raise LLMUnavailable(
                f"No Anthropic credentials found ({exc}). Add an API key in Settings."
            ) from exc

        usage = _usage(message)
        fell_back = any(
            getattr(entry, "type", "") == "fallback_message"
            for entry in (getattr(message.usage, "iterations", None) or [])
        )

        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            raise LLMRefusal(
                "The model declined this request.",
                category=getattr(details, "category", None),
            )
        if message.stop_reason == "max_tokens":
            raise LLMTruncated(f"The answer exceeded {max_tokens:,} tokens and was cut off.")

        text = next((b.text for b in message.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"The model returned malformed JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise LLMError("The model returned JSON that is not an object.")

        log.info(
            "LLM %s answered (%s): %d in, %d out, %d cached%s",
            message.model, message.stop_reason, usage.input_tokens, usage.output_tokens,
            usage.cache_read_tokens, ", via fallback" if fell_back else "",
        )
        return LLMResponse(data=data, model=message.model, usage=usage, fell_back=fell_back)


def _usage(message: Any) -> Usage:
    u = message.usage
    return Usage(
        input_tokens=u.input_tokens or 0,
        output_tokens=u.output_tokens or 0,
        cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
    )
