"""Trade Terminal as a local MCP server (stdio), for Claude Desktop and other clients (P3.5).

It is a thin client of the running app's local API, not a second copy of the
app: DuckDB allows one writer process, and going through the API means every
tool gets the same validation, job runner, run history and trial counting the
UI gets.

Safety rules, enforced in code:

* The tools are a fixed list, and each calls only the API routes in
  `ALLOWED`. Nothing here can start paper or live trading or place an order;
  `test_mcp.py` checks both the routes and that this package imports nothing
  from the execution or live layers.
* Running a backtest or a scan returns a job id; the client polls
  `job_status` and can `cancel_job`. Reports are files on this machine,
  returned as a path with headline numbers, not raw trade lists.
* The client model writes strategy and scan documents itself, from the
  language the `strategy_language` / `scan_language` tools describe; the
  validators are the same ones the app uses, and their read-back is generated
  by code, so the client can show the trader exactly what will run.
"""

from __future__ import annotations

import os
import re
from typing import Any, Literal

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

DEFAULT_URL = "http://127.0.0.1:8000"

# Every route a tool may call: (method, path pattern). Nothing else is reachable.
ALLOWED: tuple[tuple[str, str], ...] = (
    ("POST", r"/api/strategies/validate"),
    ("POST", r"/api/scans/validate"),
    ("POST", r"/api/jobs/backtest"),
    ("POST", r"/api/jobs/scan"),
    ("GET", r"/api/jobs/[\w-]+"),
    ("POST", r"/api/jobs/[\w-]+/cancel"),
    ("GET", r"/api/runs/[\w-]+/report"),
    ("GET", r"/api/strategies"),
    ("GET", r"/api/health"),
)

INSTRUCTIONS = """Trade Terminal backtests and scans Indian-market strategies on this computer.
It cannot place orders or start trading; no tool here does.

To backtest an idea: read strategy_language, write a strategy document, call validate_strategy and
show the trader its read-back (that is exactly what will run), then run_backtest, poll job_status
until it finishes, and call get_report for the HTML report and headline numbers. To screen stocks:
scan_language, validate_scan, run_scan, job_status.

Every backtest counts as a trial of that idea, and the report says how many there have been: the
more variants tried, the more of the best result is luck. Say so when you present results, and do
not present a backtest as a recommendation to trade."""

RO = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
RUNS = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)


class Api:
    """HTTP calls to the app, limited to ALLOWED."""

    def __init__(self, client: httpx.Client | None = None, base_url: str | None = None) -> None:
        self.base_url = (base_url or os.environ.get("TRADE_TERMINAL_URL") or DEFAULT_URL).rstrip("/")
        self.client = client or httpx.Client(base_url=self.base_url, timeout=60.0)

    def call(self, method: str, path: str, body: Any = None) -> Any:
        if not any(m == method and re.fullmatch(p, path) for m, p in ALLOWED):
            raise ToolError(f"{method} {path} is not reachable from this server.")
        try:
            response = self.client.request(method, path, json=body)
        except httpx.TransportError as exc:
            raise ToolError(
                f"Trade Terminal is not running at {self.base_url}. Start it with "
                f"`trade-terminal serve`, then try again. ({exc.__class__.__name__})"
            ) from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = response.text[:500]
            if isinstance(detail, dict) and detail.get("errors"):
                detail = "; ".join(f"{e.get('field') or '(document)'}: {e.get('message')}" for e in detail["errors"])
            raise ToolError(f"{response.status_code}: {detail}")
        return response.json()


def _language(text: str, marker: str) -> str:
    """The language section of a builder prompt: from its spec heading to the end."""
    i = text.find(marker)
    section = text[i:] if i >= 0 else text
    # The builder prompts ask for a JSON string inside an envelope; tools take the object itself.
    return (section
            .replace("strategy_json is the document as a JSON string, which must validate",
                     "A strategy document is a JSON object, passed as-is to the tools, which must validate")
            .replace("scan_json is the spec as a JSON string, which must validate",
                     "A scan spec is a JSON object, passed as-is to the tools, which must validate"))


def _compact_job(job: dict[str, Any]) -> dict[str, Any]:
    out = {k: job.get(k) for k in ("id", "kind", "status", "progress", "message", "error")}
    result = job.get("result")
    if job.get("status") != "succeeded" or not result:
        return out
    if job.get("kind") == "backtest":
        out["result"] = {
            "run_id": result.get("run_id"), "trials_of_this_exact_strategy": result.get("trials"),
            "metrics": {k: (result.get("metrics") or {}).get(k) for k in (
                "net_pnl", "total_return_pct", "max_drawdown_pct", "sharpe", "win_rate_pct", "total_trades")},
            "warnings": result.get("warnings"), "deflated_sharpe": result.get("deflated"),
            "next": "Call get_report with this run_id.",
        }
    elif job.get("kind") == "scan":
        rows = result.get("rows") or []
        keep = ("symbol", "status", "since", "last_fired", "close", "distance_pct", "reason")
        out["result"] = {
            "name": result.get("name"), "mode": result.get("mode"), "as_of": result.get("as_of"),
            "counts": result.get("counts"), "notes": result.get("notes"), "summary": result.get("summary"),
            "rows": [{k: r.get(k) for k in keep if r.get(k) is not None} for r in rows[:100]],
            "rows_omitted": max(0, len(rows) - 100),
        }
    else:
        out["result"] = result
    return out


def build(api: Api | None = None) -> MCPServer:
    """The server and its tools, bound to an API client (tests pass one to the app)."""
    api = api or Api()
    server = MCPServer(name="trade-terminal", title="Trade Terminal", instructions=INSTRUCTIONS)

    @server.tool(annotations=RO)
    def strategy_language() -> str:
        """The strategy document format: JSON schema, operand rules, and the indicators available."""
        from backend.agent import prompts

        return _language(prompts.builder_system(), "# The strategy document")

    @server.tool(annotations=RO)
    def scan_language() -> str:
        """The scan spec format: JSON schema, operand rules, and the indicators available."""
        from backend.agent import scan_agent

        return _language(scan_agent.system(), "# The scan spec")

    @server.tool(annotations=RO)
    def validate_strategy(document: dict[str, Any]) -> dict[str, Any]:
        """Check a strategy document. Returns errors, or its hash and a plain-words read-back
        generated from the validated document: show the read-back to the trader before running."""
        checked = api.call("POST", "/api/strategies/validate", document)
        if not checked.get("valid"):
            return {"valid": False, "errors": checked.get("errors")}
        from backend.agent import readback
        from backend.strategy.schema import Strategy

        card = readback.render(Strategy.model_validate(checked["document"]))
        return {"valid": True, "hash": checked["hash"], "read_back": card, "document": checked["document"]}

    @server.tool(annotations=RO)
    def validate_scan(spec: dict[str, Any]) -> dict[str, Any]:
        """Check a scan spec. Returns errors, or how many stocks it covers and a read-back."""
        checked = api.call("POST", "/api/scans/validate", spec)
        if not checked.get("valid"):
            return {"valid": False, "errors": checked.get("errors")}
        from backend.agent import scan_agent
        from backend.scan.spec import ScanSpec

        return {"valid": True, "stocks": checked.get("universe"),
                "read_back": scan_agent.render(ScanSpec.model_validate(checked["document"]))}

    @server.tool(annotations=RUNS)
    def run_backtest(
        document: dict[str, Any] | None = None, strategy_id: str | None = None, version: int | None = None,
        symbol: str | None = None, start: str | None = None, end: str | None = None,
        starting_capital: float | None = None, lot_size: int | None = None,
    ) -> dict[str, Any]:
        """Queue a backtest of a strategy document, or of a saved strategy by id. Returns a job id:
        poll job_status. Dates are YYYY-MM-DD. The run is recorded and counts as a trial of the idea."""
        if (document is None) == (strategy_id is None):
            raise ToolError("Give either a document or a strategy_id.")
        body = {k: v for k, v in {
            "strategy": document, "strategy_id": strategy_id, "version": version, "symbol": symbol,
            "start": start, "end": end, "starting_capital": starting_capital, "lot_size": lot_size,
            "include_chart": False,
        }.items() if v is not None}
        job = api.call("POST", "/api/jobs/backtest", body)
        return {"job_id": job["id"], "status": job["status"], "next": "Poll job_status with this job_id."}

    @server.tool(annotations=RUNS)
    def run_scan(spec: dict[str, Any], mode: Literal["now", "history"] = "now") -> dict[str, Any]:
        """Queue a scan: "now" for each stock's status on its latest bar, "history" for every past
        trigger with forward returns beside a buy-and-hold baseline. Returns a job id."""
        job = api.call("POST", "/api/jobs/scan", {"spec": spec, "mode": mode})
        return {"job_id": job["id"], "status": job["status"], "next": "Poll job_status with this job_id."}

    @server.tool(annotations=RO)
    def job_status(job_id: str) -> dict[str, Any]:
        """Progress of a backtest or scan job, and a compact result when it has finished."""
        return _compact_job(api.call("GET", f"/api/jobs/{job_id}"))

    @server.tool(annotations=RUNS)
    def cancel_job(job_id: str) -> dict[str, Any]:
        """Stop a running backtest or scan. A queued job stops at once, a running one at its next step."""
        return _compact_job(api.call("POST", f"/api/jobs/{job_id}/cancel"))

    @server.tool(annotations=RO)
    def get_report(run_id: str) -> dict[str, Any]:
        """Write a finished backtest's HTML report on this computer. Returns its path and headline
        numbers (not the raw trades), and how many backtests this idea has had."""
        return api.call("GET", f"/api/runs/{run_id}/report")

    @server.tool(annotations=RO)
    def list_strategies() -> list[dict[str, Any]]:
        """Strategies saved in the app's library: id, name, current version."""
        rows = api.call("GET", "/api/strategies").get("strategies") or []
        return [{k: r.get(k) for k in ("id", "name", "version", "updated_at")} for r in rows]

    return server


def main() -> None:
    import logging

    # stdout carries the protocol; stderr is the client's log. Keep it to what matters.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    build().run("stdio")


if __name__ == "__main__":
    main()
