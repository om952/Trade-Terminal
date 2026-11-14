"""Build the configured LLM client."""

from __future__ import annotations

from backend.core.config import settings
from backend.llm import keys
from backend.llm.base import LLMClient, LLMUnavailable


def get_client(provider: str | None = None, model: str | None = None) -> LLMClient:
    provider = provider or settings.llm_provider
    if provider == "anthropic":
        from backend.llm.anthropic_client import AnthropicClient

        return AnthropicClient(
            model=model or settings.llm_model,
            effort=settings.llm_effort,
            api_key=keys.get_key("anthropic"),
        )
    raise LLMUnavailable(f"Provider {provider!r} is not supported yet. Supported: anthropic.")


def status() -> dict:
    """What the UI shows in Settings. Never includes the key."""
    provider = settings.llm_provider
    source = keys.key_source(provider) if provider in keys.PROVIDERS else None
    return {
        "provider": provider,
        "model": settings.llm_model,
        "effort": settings.llm_effort,
        "key_source": source,
        # Without a stored key the SDK may still find an `ant auth login` profile;
        # that cannot be confirmed without a call, so it is reported as unverified.
        "configured": source is not None,
        "limits": {
            "tokens_per_compile": settings.llm_max_tokens_per_compile,
            "model_calls_per_compile": settings.llm_max_steps,
            "seconds_per_compile": settings.llm_max_seconds,
        },
    }
