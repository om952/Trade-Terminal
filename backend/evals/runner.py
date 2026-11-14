"""Run the eval set through the real builder graph and report the numbers.

    python -m backend.evals.runner --split dev --limit 10 --confirm-spend
    python -m backend.evals.runner --split holdout --confirm-spend --record
    python -m backend.evals.runner --replay evals/recordings/<file>.json

Each case goes through the same graph the app uses (compile, validate, repair,
up to the approval pause), with a counting wrapper around the model. Nothing is
saved and no backtest runs: the eval measures the compile, not the strategy.

Discipline (PRODUCTION_PLAN.md P2.1): tune prompts against `dev` only. Run
`holdout` to report a number, not to iterate; a prompt tuned on the holdout
makes the reported accuracy meaningless.

A real run spends money, so it needs `--confirm-spend`. `--record` saves the
model's answers; `--replay` re-scores them for free, which is how CI checks the
pipeline and how a scoring change is evaluated without paying again.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from backend.agent import graph as builder
from backend.core.config import IST, ROOT
from backend.evals.score import Outcome, Score, score
from backend.llm.base import LLMClient, LLMError, LLMResponse, Usage

CASES_PATH = ROOT / "evals" / "cases.json"
REPORTS_DIR = ROOT / "evals" / "reports"
RECORDINGS_DIR = ROOT / "evals" / "recordings"

# USD per million tokens: (input, output, cache read). Estimates for the cost
# column only; check current pricing before quoting them. As of 2026-11.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}
CACHE_WRITE_MULTIPLIER = 1.25


def load_cases(
    path: Path = CASES_PATH, split: str = "all", tags: list[str] | None = None,
    limit: int | None = None, ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    cases = json.loads(path.read_text())["cases"]
    if split != "all":
        cases = [c for c in cases if c["split"] == split]
    if tags:
        cases = [c for c in cases if set(tags) & set(c["tags"])]
    if ids:
        cases = [c for c in cases if c["id"] in ids]
    return cases[:limit] if limit else cases


class CountingLLM:
    """Wraps a client: totals usage and keeps every answer for recording."""

    def __init__(self, inner: LLMClient) -> None:
        self.inner = inner
        self.provider, self.model = inner.provider, inner.model
        self.usage = Usage()
        self.calls = 0
        self.answers: list[dict[str, Any]] = []
        self.models: set[str] = set()

    def complete_json(self, **kwargs: Any) -> LLMResponse:
        response = self.inner.complete_json(**kwargs)
        self.calls += 1
        self.usage = self.usage + response.usage
        self.answers.append(response.data)
        self.models.add(response.model)
        return response


def _must_not_run(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("the eval reached a save or a backtest; it must stop at approval")


def run_case(case: dict[str, Any], llm: LLMClient) -> tuple[Outcome, list[dict[str, Any]]]:
    """One prompt through the builder, to its first pause or its end."""
    counting = CountingLLM(llm)
    graph = builder.build(lambda: counting, _must_not_run, _must_not_run, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": case["id"]}}
    initial = {
        "dialogue": [{"role": "user", "content": case["prompt"]}],
        "transcript": [], "phase": "compiling", "mode": "compile", "questions_asked": 0,
        "repairs": 0, "usage": builder._fresh_usage(), "backtest": {},
        "document": None, "strategy_id": None, "run": None,
    }
    started = time.monotonic()
    repairs, error = 0, None
    try:
        for update in graph.stream(initial, config, stream_mode="updates"):
            repairs += sum(1 for node in update if node == "repair")
    except LLMError as exc:
        error = str(exc)
    seconds = time.monotonic() - started

    state = graph.get_state(config)
    values = state.values or {}
    pending = next((i.value for t in state.tasks for i in t.interrupts), None)

    if error:
        status = "failed"
    elif pending and pending.get("type") == "approval":
        status = "ready"
    elif pending and pending.get("type") == "questions":
        status = "needs_clarification"
    elif values.get("phase") == "unsupported":
        status = "unsupported"
    else:
        status = "failed"
        error = values.get("error") or "the graph ended without a result"

    outcome = Outcome(
        status=status,
        document=(pending or {}).get("document") if status == "ready" else None,
        questions=(pending or {}).get("questions", []) if status == "needs_clarification" else [],
        assumptions=values.get("assumptions") or [],
        llm_calls=counting.calls, repairs=repairs,
        input_tokens=counting.usage.input_tokens, output_tokens=counting.usage.output_tokens,
        cache_read_tokens=counting.usage.cache_read_tokens,
        seconds=round(seconds, 2), error=error,
        model=", ".join(sorted(counting.models)) or counting.model,
    )
    return outcome, counting.answers


def cost_usd(model: str, outcome: Outcome) -> float | None:
    price = PRICES.get(model)
    if price is None:
        return None
    inp, out, cache_read = price
    return (
        outcome.input_tokens * inp + outcome.output_tokens * out
        + outcome.cache_read_tokens * cache_read
    ) / 1_000_000


def summarize(cases: list[dict], outcomes: list[Outcome], scores: list[Score], model: str) -> dict[str, Any]:
    n = len(cases)
    ready = [(c, o, s) for c, o, s in zip(cases, outcomes, scores, strict=True)
             if c["expect"]["status"] == "ready"]

    def rate(hits: int, total: int) -> float | None:
        return round(100.0 * hits / total, 1) if total else None

    fields: dict[str, list[bool]] = defaultdict(list)
    for _, _, s in ready:
        for name, ok in s.fields.items():
            fields[name].append(ok)

    by_tag: dict[str, list[bool]] = defaultdict(list)
    for c, s in zip(cases, scores, strict=True):
        for tag in c["tags"]:
            by_tag[tag].append(s.exact)

    costs = [cost_usd(model, o) for o in outcomes]
    known = [c for c in costs if c is not None]
    return {
        "model": model,
        "cases": n,
        "exact_pct": rate(sum(s.exact for s in scores), n),
        "status_pct": rate(sum(s.status_ok for s in scores), n),
        "ready_cases": len(ready),
        "valid_pct": rate(sum(s.valid for _, _, s in ready), len(ready)),
        "field_pct": {k: rate(sum(v), len(v)) for k, v in sorted(fields.items())},
        "over_asked": sum(1 for c, o, _ in ready if o.status == "needs_clarification"),
        "failed": sum(1 for o in outcomes if o.status == "failed"),
        "mean_llm_calls": round(sum(o.llm_calls for o in outcomes) / n, 2) if n else None,
        "mean_repairs": round(sum(o.repairs for o in outcomes) / n, 2) if n else None,
        "mean_seconds": round(sum(o.seconds for o in outcomes) / n, 1) if n else None,
        "tokens": {
            "input": sum(o.input_tokens for o in outcomes),
            "output": sum(o.output_tokens for o in outcomes),
            "cache_read": sum(o.cache_read_tokens for o in outcomes),
        },
        "cost_usd_total": round(sum(known), 4) if known else None,
        "cost_usd_per_compile": round(sum(known) / len(known), 4) if known else None,
        "by_tag_exact_pct": {k: rate(sum(v), len(v)) for k, v in sorted(by_tag.items())},
    }


def run(cases: list[dict], llm_for_case, model: str, on_case=None) -> dict[str, Any]:
    """Run and score. `llm_for_case(case)` returns the client to use for it."""
    outcomes, scores, recordings = [], [], {}
    for case in cases:
        outcome, answers = run_case(case, llm_for_case(case))
        result = score(case, outcome)
        outcomes.append(outcome)
        scores.append(result)
        recordings[case["id"]] = answers
        if on_case:
            on_case(case, outcome, result)
    return {
        "summary": summarize(cases, outcomes, scores, model),
        "results": [
            {**s.as_dict(), "split": c["split"], "tags": c["tags"], "prompt": c["prompt"],
             "outcome": {k: v for k, v in asdict(o).items() if k != "document"},
             "document": o.document}
            for c, o, s in zip(cases, outcomes, scores, strict=True)
        ],
        "recordings": recordings,
    }


def markdown(report: dict[str, Any], split: str, source: str) -> str:
    s = report["summary"]
    lines = [
        f"# Builder eval: {s['model']}, split `{split}`",
        "",
        f"{source}. {s['cases']} cases, {s['ready_cases']} of them compilable.",
        "",
        "| Measure | Value |", "|---|---|",
        f"| Exact (status and every scored field) | {s['exact_pct']}% |",
        f"| Right kind of answer (compile / ask / decline) | {s['status_pct']}% |",
        f"| Valid document, of compilable cases | {s['valid_pct']}% |",
        f"| Asked a question when none was needed | {s['over_asked']} |",
        f"| Failed outright | {s['failed']} |",
        f"| Model calls per case | {s['mean_llm_calls']} |",
        f"| Repair loops per case | {s['mean_repairs']} |",
        f"| Seconds per case | {s['mean_seconds']} |",
        f"| Estimated cost per compile (USD) | {s['cost_usd_per_compile']} |",
        f"| Estimated total cost (USD) | {s['cost_usd_total']} |",
        "", "## By field (compilable cases with a valid document)", "",
        "| Field | Correct |", "|---|---|",
        *[f"| {k} | {v}% |" for k, v in s["field_pct"].items()],
        "", "## By tag", "", "| Tag | Exact |", "|---|---|",
        *[f"| {k} | {v}% |" for k, v in s["by_tag_exact_pct"].items()],
        "", "## Misses", "",
    ]
    misses = [r for r in report["results"] if not r["exact"]]
    if not misses:
        lines.append("None.")
    for r in misses:
        lines.append(f"- **{r['case_id']}** (expected {r['expected_status']}, got {r['status']})"
                     + (f": {r['outcome']['error']}" if r["outcome"]["error"] else ""))
        for name, diff in r["mismatches"].items():
            lines.append(f"  - {name}: expected `{diff.get('expected') if isinstance(diff, dict) else ''}`,"
                         f" got `{diff.get('actual') if isinstance(diff, dict) else diff}`")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="evals", description="Strategy builder compile-accuracy eval")
    p.add_argument("--split", choices=["dev", "holdout", "all"], default="dev")
    p.add_argument("--limit", type=int)
    p.add_argument("--tag", action="append", help="only cases with this tag (repeatable)")
    p.add_argument("--id", action="append", help="only this case id (repeatable)")
    p.add_argument("--model", help="override LLM_MODEL")
    p.add_argument("--record", action="store_true", help="save the model's answers for replay")
    p.add_argument("--replay", type=Path, help="score recorded answers instead of calling a model")
    p.add_argument("--confirm-spend", action="store_true", help="required for a real model run")
    p.add_argument("--no-report", action="store_true")
    args = p.parse_args(argv)

    cases = load_cases(split=args.split, tags=args.tag, limit=args.limit, ids=args.id)
    if not cases:
        print("No cases match.")
        return 1

    if args.replay:
        from backend.llm.fake import FakeLLM

        recorded = json.loads(args.replay.read_text())
        model, answers = recorded["model"], recorded["answers"]
        cases = [c for c in cases if c["id"] in answers]
        source = f"Replayed from {args.replay.name}"

        def llm_for_case(case):
            return FakeLLM(list(answers[case["id"]]), model=model, usage_per_call=Usage())
    else:
        from backend.core.config import settings
        from backend.llm.factory import get_client

        model = args.model or settings.llm_model
        if not args.confirm_spend:
            print(f"This would send {len(cases)} case(s) to {model} and spend real money.\n"
                  f"Re-run with --confirm-spend to proceed.")
            return 2
        client = get_client(model=model)
        source = f"Live run {datetime.now(IST):%Y-%m-%d %H:%M}"

        def llm_for_case(case):
            return client

    def progress(case, outcome, result):
        mark = "ok  " if result.exact else "MISS"
        print(f"  {mark} {case['id']:<32} {outcome.status:<20} calls={outcome.llm_calls} "
              f"repairs={outcome.repairs} {outcome.seconds:.1f}s", flush=True)

    print(f"Running {len(cases)} case(s), split {args.split}, model {model}")
    report = run(cases, llm_for_case, model, on_case=progress)
    s = report["summary"]
    print(f"\nExact {s['exact_pct']}%  status {s['status_pct']}%  valid {s['valid_pct']}%  "
          f"cost/compile {s['cost_usd_per_compile']}  total {s['cost_usd_total']}")

    stamp = f"{datetime.now(IST):%Y%m%d-%H%M}-{model.replace(':', '_')}-{args.split}"
    if args.record and not args.replay:
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        path = RECORDINGS_DIR / f"{stamp}.json"
        path.write_text(json.dumps({"model": model, "split": args.split,
                                    "answers": report["recordings"]}, indent=1))
        print(f"Recorded answers: {path}")
    if not args.no_report:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        body = {k: v for k, v in report.items() if k != "recordings"}
        (REPORTS_DIR / f"{stamp}.json").write_text(json.dumps(body, indent=1, default=str))
        (REPORTS_DIR / f"{stamp}.md").write_text(markdown(report, args.split, source))
        print(f"Report: {REPORTS_DIR / (stamp + '.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
