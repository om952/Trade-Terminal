"""P3.5: the MCP server, through the real API, the real job runner and a real stdio session.

The gate asks that a client complete compile, backtest and report through MCP,
and that job cancel works. Here the "client" is the test, playing the part a
model plays: read the language, write a document, validate, run, poll, report.
"""

from __future__ import annotations

import ast
import asyncio
import json
import sys
import threading
import time
from datetime import date
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from backend.core.config import ROOT, settings
from backend.mcpserver import server as mcp_server
from backend.strategy import templates

SYMBOL = "SIM|MCP"


@pytest.fixture
def tools(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api.main import app
    from backend.appstate import db
    from backend.data import store
    from backend.data.simulator import generate_ohlcv
    from backend.services.jobs import runner

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    store.close_db()
    store.write_candles(SYMBOL, "15m", generate_ohlcv("NIFTY", date(2025, 1, 1), date(2025, 6, 30), "15m", seed=9),
                        source="simulated")
    store.write_candles(SYMBOL, "1d", generate_ohlcv("NIFTY", date(2024, 1, 1), date(2025, 6, 30), "1d", seed=9),
                        source="simulated")
    runner.start()
    client = TestClient(app, base_url="http://127.0.0.1:8000")
    server = mcp_server.build(mcp_server.Api(client=client))
    yield server, runner
    runner.shutdown(timeout=10)
    db.close()
    store.close_db()


def call(server, name: str, **args):
    """Call a tool as a client would, and return its structured result."""
    result = asyncio.run(server.call_tool(name, args))
    assert not result.is_error, result.content[0].text
    data = result.structured_content
    return data["result"] if isinstance(data, dict) and set(data) == {"result"} else data


def wait(server, job_id: str, timeout: float = 60) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = call(server, "job_status", job_id=job_id)
        if status["status"] in ("succeeded", "failed", "cancelled", "interrupted"):
            return status
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} did not finish")


def _document() -> dict:
    doc = templates.load("ema_crossover", symbol=SYMBOL).model_dump(mode="json")
    doc.update({"id": "", "name": "EMA cross via MCP", "timeframe": "15m"})
    return doc


def test_compile_backtest_report_through_mcp(tools):
    server, _ = tools
    language = call(server, "strategy_language")
    assert language.startswith("# The strategy document") and "JSON object, passed as-is" in language
    assert "EMA" in language and "strike_rule" in language

    bad = call(server, "validate_strategy", document={**_document(), "entry": {"op": "AND", "conditions": [
        {"left": "ema_nine", "cmp": ">", "right": 0}]}})
    assert bad["valid"] is False and "ema_nine" in json.dumps(bad["errors"])

    good = call(server, "validate_strategy", document=_document())
    assert good["valid"] and good["read_back"]["title"] == "EMA cross via MCP"
    assert any(s["heading"] == "Enter when" for s in good["read_back"]["sections"])

    job = call(server, "run_backtest", document=good["document"])
    done = wait(server, job["job_id"])
    assert done["status"] == "succeeded", done
    run_id = done["result"]["run_id"]
    assert done["result"]["metrics"]["total_trades"] > 0
    assert "trades" not in json.dumps(done["result"]).replace("total_trades", "")

    report = call(server, "get_report", run_id=run_id)
    path = Path(report["path"])
    assert path.exists() and path.parent == settings.data_dir / "reports"
    html = path.read_text()
    assert "EMA cross via MCP" in html and "Synthetic data" in html and "<script" not in html
    assert report["headline"]["total_trades"] == done["result"]["metrics"]["total_trades"]
    assert report["synthetic_data"] is True and report["family_trials"]["runs"] == 1


def test_a_saved_strategy_can_be_run_by_id(tools):
    from backend.appstate import strategies as saved

    server, _ = tools
    version, _ = saved.save(_document())
    listed = call(server, "list_strategies")
    assert [s["id"] for s in listed] == [version.strategy_id]
    done = wait(server, call(server, "run_backtest", strategy_id=version.strategy_id)["job_id"])
    assert done["status"] == "succeeded"


def test_cancel_stops_a_running_job(tools, monkeypatch):
    from backend.services import jobs as job_module

    server, _ = tools
    started, release = threading.Event(), threading.Event()
    real = job_module.backtest_and_record

    def slow(*args, **kwargs):
        started.set()
        release.wait(10)
        kwargs["on_progress"](0.5, "half way")     # where a real run would notice the cancel
        return real(*args, **kwargs)

    monkeypatch.setattr(job_module, "backtest_and_record", slow)
    job_id = call(server, "run_backtest", document=_document())["job_id"]
    assert started.wait(10)
    call(server, "cancel_job", job_id=job_id)
    release.set()
    assert wait(server, job_id)["status"] == "cancelled"


def test_scans_through_mcp(tools):
    server, _ = tools
    assert call(server, "scan_language").startswith("# The scan spec")
    spec = {"name": "Above 50-day SMA", "universe": {"symbols": [SYMBOL, "SIM|NOTHING"]},
            "indicators": [{"id": "sma50", "type": "SMA", "params": {"period": 50}}],
            "condition": {"op": "AND", "conditions": [{"left": "close", "cmp": ">", "right": "sma50"}]}}
    checked = call(server, "validate_scan", spec=spec)
    assert checked["valid"] and checked["stocks"] == 2 and checked["read_back"]["title"] == "Above 50-day SMA"

    done = wait(server, call(server, "run_scan", spec=spec)["job_id"])
    assert done["status"] == "succeeded"
    rows = {r["symbol"]: r for r in done["result"]["rows"]}
    assert rows["SIM|NOTHING"]["status"] == "no_data" and rows[SYMBOL]["status"] in (
        "triggered_today", "holding", "lapsed", "waiting")


def test_errors_are_readable():
    server = mcp_server.build(mcp_server.Api(base_url="http://127.0.0.1:9"))
    with pytest.raises(ToolError, match="not running.*trade-terminal serve"):
        asyncio.run(server.call_tool("job_status", {"job_id": "job_x"}))
    with pytest.raises(ToolError, match="either a document or a strategy_id"):
        asyncio.run(server.call_tool("run_backtest", {}))


# ---------------------------------------------------------------------------
# What the server can and cannot reach
# ---------------------------------------------------------------------------

def test_no_route_outside_the_allowlist_is_reachable():
    api = mcp_server.Api(base_url="http://127.0.0.1:9")
    for method, path in (("POST", "/api/live/start"), ("POST", "/api/live/abc/stop"), ("PUT", "/api/llm/key"),
                         ("POST", "/api/strategies"), ("DELETE", "/api/strategies/x"), ("POST", "/api/jobs/fetch"),
                         ("GET", "/api/jobs/../live/sessions")):
        with pytest.raises(ToolError, match="not reachable"):
            api.call(method, path)
    assert not any("live" in p or "credentials" in p or "llm" in p for _, p in mcp_server.ALLOWED)


def test_the_server_imports_nothing_that_can_trade():
    forbidden = ("backend.execution", "backend.live", "backend.data.upstox_auth")
    for path in Path(mcp_server.__file__).parent.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = ([node.module] if isinstance(node, ast.ImportFrom) and node.module else
                     [a.name for a in node.names] if isinstance(node, ast.Import) else [])
            assert not any(n.startswith(forbidden) for n in names), (path.name, names)


def test_tools_are_a_fixed_list():
    names = [t.name for t in asyncio.run(mcp_server.build(mcp_server.Api(base_url="http://x")).list_tools())]
    assert names == ["strategy_language", "scan_language", "validate_strategy", "validate_scan", "run_backtest",
                     "run_scan", "job_status", "cancel_job", "get_report", "list_strategies"]


# ---------------------------------------------------------------------------
# A real client over stdio, as Claude Desktop runs it
# ---------------------------------------------------------------------------

def test_a_real_stdio_session(tmp_path):
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async def session() -> tuple[list[str], str, str]:
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "backend.mcpserver"], cwd=str(ROOT),
            env={"TRADE_TERMINAL_URL": "http://127.0.0.1:9", "DATA_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"},
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
            await client.initialize()
            tools = [t.name for t in (await client.list_tools()).tools]
            language = await client.call_tool("scan_language", {})
            offline = await client.call_tool("job_status", {"job_id": "job_x"})
            assert offline.is_error
            return tools, language.content[0].text, offline.content[0].text

    tools, language, offline = asyncio.run(asyncio.wait_for(session(), timeout=60))
    assert "run_backtest" in tools and len(tools) == 10
    assert language.startswith("# The scan spec")
    assert "not running" in offline
