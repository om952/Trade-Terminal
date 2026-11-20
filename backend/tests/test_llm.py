"""P1.3: the LLM layer. No network: the SDK client is a stub that records requests."""

from __future__ import annotations

import io
import json
import logging
from contextlib import contextmanager
from types import SimpleNamespace

import keyring
import pytest

from backend.core import logsetup
from backend.llm import keys
from backend.llm.anthropic_client import FALLBACK_BETA, AnthropicClient
from backend.llm.base import LLMError, LLMRefusal, LLMTruncated, Usage
from backend.llm.budget import Budget, BudgetExceeded
from backend.llm.fake import FakeLLM

SCHEMA = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"],
          "additionalProperties": False}


class StubSDK:
    """Mimics client.beta.messages.stream(...).get_final_message()."""

    def __init__(self, message):
        self.message = message
        self.requests: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    @contextmanager
    def _stream(self, **kwargs):
        self.requests.append(kwargs)
        yield SimpleNamespace(get_final_message=lambda: self.message)


def _message(text='{"x": 1}', stop="end_turn", iterations=None, category=None):
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        stop_reason=stop,
        stop_details=SimpleNamespace(category=category) if category else None,
        model="claude-opus-5-5",
        usage=SimpleNamespace(
            input_tokens=1200, output_tokens=300, cache_read_input_tokens=5000,
            cache_creation_input_tokens=0, iterations=iterations,
        ),
    )


def _client(message):
    sdk = StubSDK(message)
    return AnthropicClient(model="claude-opus-5-5", effort="high", sdk_client=sdk), sdk


def test_request_shape():
    client, sdk = _client(_message())
    response = client.complete_json(
        system="SYSTEM", messages=[{"role": "user", "content": "hi"}], schema=SCHEMA
    )
    sent = sdk.requests[0]
    assert sent["model"] == "claude-opus-5-5"
    assert sent["output_config"] == {"effort": "high", "format": {"type": "json_schema", "schema": SCHEMA}}
    assert sent["fallbacks"] == "default" and sent["betas"] == [FALLBACK_BETA]
    assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "thinking" not in sent and "temperature" not in sent
    assert response.data == {"x": 1}
    assert response.usage == Usage(1200, 300, 5000, 0)
    assert not response.fell_back


def test_fallback_is_detected():
    client, _ = _client(_message(iterations=[SimpleNamespace(type="message"),
                                             SimpleNamespace(type="fallback_message")]))
    assert client.complete_json(system="", messages=[], schema=SCHEMA).fell_back


def test_refusal_truncation_and_bad_json_raise():
    with pytest.raises(LLMRefusal) as refusal:
        _client(_message(stop="refusal", category="cyber"))[0].complete_json(
            system="", messages=[], schema=SCHEMA)
    assert refusal.value.category == "cyber"
    with pytest.raises(LLMTruncated):
        _client(_message(stop="max_tokens"))[0].complete_json(system="", messages=[], schema=SCHEMA)
    with pytest.raises(LLMError, match="malformed"):
        _client(_message(text="{nope"))[0].complete_json(system="", messages=[], schema=SCHEMA)
    with pytest.raises(LLMError, match="not an object"):
        _client(_message(text="[1]"))[0].complete_json(system="", messages=[], schema=SCHEMA)


def test_budget_caps_steps_tokens_and_time():
    b = Budget(max_tokens=10_000, max_steps=2, max_seconds=60)
    b.check()
    b.charge(Usage(4000, 1000))
    b.check()
    b.charge(Usage(1, 1))
    with pytest.raises(BudgetExceeded, match="2 model calls"):
        b.check()

    tokens = Budget(max_tokens=1000, max_steps=10, max_seconds=60)
    tokens.charge(Usage(900, 200))
    with pytest.raises(BudgetExceeded, match="tokens"):
        tokens.check()

    clock = Budget(max_tokens=1000, max_steps=10, max_seconds=0.0)
    with pytest.raises(BudgetExceeded, match="limit 0s"):
        clock.check()
    assert set(b.snapshot()) >= {"steps", "tokens", "seconds", "max_steps"}


def test_fake_llm_scripts_answers_and_errors():
    fake = FakeLLM([{"a": 1}, LLMError("boom"), lambda req: {"echo": req["messages"][-1]["content"]}])
    assert fake.complete_json(system="s", messages=[], schema={}).data == {"a": 1}
    with pytest.raises(LLMError):
        fake.complete_json(system="s", messages=[], schema={})
    out = fake.complete_json(system="s", messages=[{"role": "user", "content": "q"}], schema={})
    assert out.data == {"echo": "q"} and len(fake.calls) == 3


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------

KEY = "sk-ant-api03-" + "x" * 40


@pytest.fixture
def clean_keys(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    keys.delete_key("anthropic")
    yield
    keys.delete_key("anthropic")


def test_keys_live_in_the_keychain(clean_keys):
    assert keys.get_key("anthropic") is None and keys.key_source("anthropic") is None
    keys.set_key("anthropic", f"  {KEY}\n")
    assert keyring.get_password(keys.SERVICE, "anthropic-api-key") == KEY
    assert keys.get_key("anthropic") == KEY and keys.key_source("anthropic") == "keychain"
    assert keys.delete_key("anthropic") and not keys.delete_key("anthropic")


def test_environment_is_the_fallback(clean_keys, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    assert keys.key_source("anthropic") == "environment" and keys.get_key("anthropic") == KEY


def test_bad_keys_and_providers_are_refused(clean_keys):
    with pytest.raises(ValueError):
        keys.set_key("anthropic", "short")
    with pytest.raises(ValueError):
        keys.set_key("anthropic", "has a space in it which keys never do")
    with pytest.raises(ValueError):
        keys.get_key("nope")


def test_a_keychain_key_is_redacted_from_logs(clean_keys):
    keys.set_key("anthropic", KEY)
    stream = io.StringIO()
    root = logging.getLogger()
    level = root.level
    try:
        logsetup.configure_logging("INFO", stream=stream, to_file=False)
        logging.getLogger("backend.test").info("calling with key %s", KEY)
    finally:
        logsetup.reset_logging()
        root.setLevel(level)
    assert KEY not in stream.getvalue() and logsetup.REDACTED in stream.getvalue()


def test_status_never_contains_the_key(clean_keys):
    from backend.llm import factory

    keys.set_key("anthropic", KEY)
    status = factory.status()
    assert status["configured"] and status["key_source"] == "keychain"
    assert KEY not in json.dumps(status)


def test_key_endpoints(clean_keys):
    from fastapi.testclient import TestClient

    from backend.api.main import app

    client = TestClient(app, base_url="http://127.0.0.1:8000")
    assert client.get("/api/llm/status").json()["configured"] is False
    stored = client.put("/api/llm/key", json={"api_key": KEY})
    assert stored.status_code == 200 and stored.json()["key_source"] == "keychain"
    assert KEY not in stored.text
    assert client.put("/api/llm/key", json={"api_key": "x" * 19}).status_code == 422
    assert client.delete("/api/llm/key").json()["configured"] is False


def test_scripted_provider_replays_a_file(tmp_path, monkeypatch):
    from backend.core.config import settings
    from backend.llm import factory, scripted

    script = tmp_path / "script.json"
    script.write_text(json.dumps([{"a": 1}, {"b": 2}]))
    monkeypatch.setattr(settings, "llm_provider", "scripted")
    monkeypatch.setattr(settings, "llm_script", script)
    monkeypatch.setattr(scripted, "_instances", {})

    assert factory.status()["configured"] and factory.status()["model"] == "scripted:script.json"
    assert factory.get_client().complete_json(system="", messages=[], schema={}).data == {"a": 1}
    assert factory.get_client().complete_json(system="", messages=[], schema={}).data == {"b": 2}
    with pytest.raises(LLMError, match="no answer left"):
        factory.get_client().complete_json(system="", messages=[], schema={})


def test_scripted_provider_answers_each_kind_of_call_in_its_own_order(tmp_path):
    from backend.llm.scripted import ScriptedLLM

    script = tmp_path / "script.json"
    script.write_text(json.dumps([
        {"explanation": "first explanation", "caveats": []},
        {"status": "ready", "scan_json": "{}", "questions": [], "assumptions": [], "unsupported_reason": ""},
        {"explanation": "second explanation", "caveats": []},
    ]))
    llm = ScriptedLLM(script)
    scan_schema = {"required": ["status", "scan_json"]}
    explain_schema = {"required": ["explanation", "caveats"]}
    assert llm.complete_json(system="", messages=[], schema=scan_schema).data["scan_json"] == "{}"
    assert llm.complete_json(system="", messages=[], schema=explain_schema).data["explanation"] == "first explanation"
    assert llm.complete_json(system="", messages=[], schema=explain_schema).data["explanation"] == "second explanation"
    with pytest.raises(LLMError, match="no answer left"):
        llm.complete_json(system="", messages=[], schema=scan_schema)
