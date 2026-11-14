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
    if provider == "scripted":
        from backend.llm.scripted import scripted_client

        if settings.llm_script is None or not settings.llm_script.exists():
            raise LLMUnavailable("LLM_PROVIDER=scripted needs LLM_SCRIPT pointing at a JSON file.")
        return scripted_client(settings.llm_script)
    raise LLMUnavailable(f"Provider {provider!r} is not supported yet. Supported: anthropic.")


def status() -> dict:
    """What the UI shows in Settings. Never includes the key."""
    provider = settings.llm_provider
    if provider == "scripted":
        source = "scripted" if settings.llm_script and settings.llm_script.exists() else None
        model = f"scripted:{settings.llm_script.name}" if settings.llm_script else "scripted"
    else:
        source = keys.key_source(provider) if provider in keys.PROVIDERS else None
        model = settings.llm_model
    return {
        "provider": provider,
        "model": model,
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
