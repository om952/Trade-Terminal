"""The builder graph: describe, clarify, compile, validate, approve, backtest, explain, refine.

    compile ──► clarify (pause: at most 2 questions) ──► compile
       │
       ▼
    validate ──► repair (at most 3) ──► validate
       │
       ▼
    readback (pause: approve / change / edit / reject)
       │ approve                    │ change
       ▼                            ▼
     save ─► backtest ─► explain ─► followup (pause) ─► refine ─► validate ...

Rules this file enforces in code, not in prompts:

* No backtest before approval: the only edge into `save` and `backtest` is the
  approve branch of `readback`.
* The read-back is rendered from the validated document (readback.py).
* The model never acts. Each node calls the model for data and then the node's
  code decides what happens; the only side effects are saving a strategy
  version and running a backtest, both through the same services the UI uses.
* Every model call is checked against the turn's budget first.
* The explanation may only quote computed numbers (numbers.py).
* Nodes that pause (`interrupt`) have no side effects before the pause,
  because LangGraph re-runs a paused node from its start on resume.

State is checkpointed in SQLite after every node, so a restart resumes at the
same pause with the same document.
"""

from __future__ import annotations

import json
import logging
import operator
from collections.abc import Callable
from datetime import date
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from backend.agent import numbers, prompts, readback
from backend.agent.patch import PatchError, apply_patch, decode_ops
from backend.llm.base import LLMClient, LLMError, LLMRefusal, Usage
from backend.llm.budget import Budget, BudgetExceeded
from backend.strategy.schema import Strategy

log = logging.getLogger(__name__)

MAX_REPAIRS = 3

Phase = Literal[
    "compiling", "awaiting_answers", "awaiting_approval", "backtesting", "awaiting_followup",
    "rejected", "unsupported", "failed",
]


class BuilderState(TypedDict, total=False):
    # What the UI renders: an append-only list of events.
    transcript: Annotated[list[dict[str, Any]], operator.add]
    # Model context for compiling: user turns and the model's questions.
    dialogue: list[dict[str, str]]
    phase: str
    mode: str                         # compile | refine
    request: str                      # the change request being refined
    questions: list[dict[str, Any]]
    questions_asked: int
    assumptions: list[str]
    candidate_text: str               # model output awaiting validation
    errors: list[dict[str, str]]
    repairs: int
    document: dict[str, Any] | None   # last valid document
    strategy_id: str | None
    version: int | None
    backtest: dict[str, Any]          # parameters for runs
    run: dict[str, Any] | None
    usage: dict[str, int]             # this turn's spend
    error: str | None


def _event(role: str, kind: str, text: str = "", **data: Any) -> dict[str, Any]:
    return {"role": role, "kind": kind, "text": text, **({"data": data} if data else {})}


# ---------------------------------------------------------------------------
# Budget, carried in state so it survives a restart mid-turn
# ---------------------------------------------------------------------------

def _budget(state: BuilderState) -> Budget:
    b = Budget.from_settings()
    u = state.get("usage") or {}
    b.steps = u.get("steps", 0)
    b.used = Usage(u.get("input", 0), u.get("output", 0), u.get("cache_read", 0), u.get("cache_write", 0))
    b.started -= u.get("seconds", 0.0)
    return b


def _usage(b: Budget) -> dict[str, Any]:
    return {
        "steps": b.steps, "input": b.used.input_tokens, "output": b.used.output_tokens,
        "cache_read": b.used.cache_read_tokens, "cache_write": b.used.cache_write_tokens,
        "seconds": round(b.elapsed, 2), "total": b.used.total,
    }


def _fresh_usage() -> dict[str, Any]:
    return {"steps": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "seconds": 0.0}


def _call(llm: LLMClient, state: BuilderState, *, system: str, messages, schema) -> tuple[dict, dict]:
    """One budgeted model call. Returns (answer, usage-after)."""
    budget = _budget(state)
    budget.check()
    response = llm.complete_json(system=system, messages=messages, schema=schema)
    budget.charge(response.usage)
    return response.data, _usage(budget)


def _failure(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, BudgetExceeded):
        text = f"I stopped to stay within this turn's limits. {exc}"
    elif isinstance(exc, LLMRefusal):
        text = "The model declined this request. Try rephrasing the strategy."
    else:
        text = f"The model call failed: {exc}"
    return {"phase": "failed", "error": text, "transcript": [_event("assistant", "error", text)]}


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

def make_nodes(get_llm: Callable[[], LLMClient], run_backtest: Callable[..., dict], save: Callable[..., tuple[str, int]]):
    """Node functions closed over their collaborators, so tests can swap them."""

    def compile_node(state: BuilderState) -> dict[str, Any]:
        dialogue = list(state.get("dialogue") or [])
        asked = state.get("questions_asked", 0)
        if asked >= prompts.MAX_QUESTIONS:
            dialogue.append({"role": "user", "content": (
                "No more questions are allowed. Compile the strategy now, choosing defaults "
                "and listing them in assumptions."
            )})
        try:
            answer, usage = _call(
                get_llm(), state, system=prompts.builder_system(),
                messages=dialogue, schema=prompts.COMPILE_SCHEMA,
            )
        except LLMError as exc:
            return _failure(exc)
        return _envelope(state, answer, usage, asked)

    def clarify_node(state: BuilderState) -> dict[str, Any]:
        questions = state.get("questions") or []
        reply = interrupt({"type": "questions", "questions": questions})
        text = _answers_text(questions, reply)
        dialogue = list(state.get("dialogue") or [])
        dialogue.append({"role": "user", "content": text})
        return {
            "dialogue": dialogue, "questions": [], "phase": "compiling",
            "usage": _fresh_usage(),
            "transcript": [_event("user", "answers", text)],
        }

    def validate_node(state: BuilderState) -> dict[str, Any]:
        text = state.get("candidate_text", "")
        try:
            document = json.loads(text)
            if not isinstance(document, dict):
                raise ValueError("the document must be a JSON object")
        except ValueError as exc:
            return {"errors": [{"field": "", "message": f"Not valid JSON: {exc}"}]}
        document = _normalize(document)
        try:
            strategy = Strategy.model_validate(document)
        except Exception as exc:  # pydantic ValidationError
            from backend.services.backtest import format_validation_error

            errors = format_validation_error(exc)
            update: dict[str, Any] = {"errors": errors}
            if state.get("repairs", 0) >= MAX_REPAIRS and state.get("document"):
                # Going back to the last good document: say why the new one was dropped.
                listed = "; ".join(f"{e['field']}: {e['message']}" for e in errors[:5])
                update["transcript"] = [_event(
                    "assistant", "error", f"That version is not valid, so it was not applied: {listed}"
                )]
                update["candidate_text"] = ""
            return update
        valid = strategy.model_dump(mode="json")
        if state.get("strategy_id"):
            valid["id"] = state["strategy_id"]
        return {"errors": [], "document": valid, "repairs": 0}

    def repair_node(state: BuilderState) -> dict[str, Any]:
        errors = "\n".join(f"- {e['field'] or '(document)'}: {e['message']}" for e in state["errors"])
        messages = list(state.get("dialogue") or []) + [
            {"role": "assistant", "content": state.get("candidate_text", "")},
            {"role": "user", "content": (
                "That document failed validation:\n" + errors + "\n\nReturn the complete "
                "corrected document with status \"ready\". Change only what the errors require."
            )},
        ]
        try:
            answer, usage = _call(
                get_llm(), state, system=prompts.builder_system(),
                messages=messages, schema=prompts.COMPILE_SCHEMA,
            )
        except LLMError as exc:
            return _failure(exc)
        update: dict[str, Any] = {"repairs": state.get("repairs", 0) + 1, "usage": usage}
        if answer.get("status") == "ready" and answer.get("strategy_json"):
            update["candidate_text"] = answer["strategy_json"]
        return update

    def readback_node(state: BuilderState) -> dict[str, Any]:
        strategy = Strategy.model_validate(state["document"])
        card = readback.render(strategy)
        decision = interrupt({
            "type": "approval",
            "card": card,
            "assumptions": state.get("assumptions") or [],
            "document": state["document"],
        })
        action = (decision or {}).get("action") if isinstance(decision, dict) else decision
        if action == "approve":
            return {
                "phase": "backtesting", "usage": _fresh_usage(),
                "transcript": [_event("user", "approval", "Approved", hash=strategy.content_hash())],
            }
        if action == "change":
            text = str(decision.get("text", "")).strip()
            return {
                "mode": "refine", "request": text, "phase": "compiling", "usage": _fresh_usage(),
                "candidate_text": "",
                "transcript": [_event("user", "text", text)],
            }
        if action == "edit":
            return {
                "candidate_text": json.dumps(decision.get("document", {})), "phase": "compiling",
                "repairs": MAX_REPAIRS,    # a hand edit is not sent back to the model to fix
                "transcript": [_event("user", "edit", "Edited the document directly")],
            }
        return {"phase": "rejected", "transcript": [_event("user", "reject", "Rejected")]}

    def save_node(state: BuilderState) -> dict[str, Any]:
        current = state.get("document") or {}
        strategy_id, version = save(current, state.get("strategy_id"))
        document = dict(current, id=strategy_id)
        return {
            "strategy_id": strategy_id, "version": version, "document": document,
            "transcript": [_event("system", "saved", f"Saved as version {version}",
                                  strategy_id=strategy_id, version=version)],
        }

    def backtest_node(state: BuilderState) -> dict[str, Any]:
        try:
            report = run_backtest(state["document"], state.get("backtest") or {}, state.get("strategy_id"))
        except Exception as exc:
            text = f"The backtest could not run: {exc}"
            return {
                "run": None, "phase": "awaiting_followup",
                "transcript": [_event("assistant", "error", text)],
            }
        run = _run_summary(report)
        return {"run": run, "transcript": [_event("system", "result", "", run=run)]}

    def explain_node(state: BuilderState) -> dict[str, Any]:
        run = state.get("run")
        if not run:
            return {"phase": "awaiting_followup"}
        facts = _explain_facts(state, run)
        messages = [{"role": "user", "content": (
            "Backtest results (JSON):\n" + json.dumps(facts, sort_keys=True)
            + "\n\nExplain them to the trader."
        )}]
        text, caveats, usage = "", [], state.get("usage") or _fresh_usage()
        for _attempt in range(2):
            try:
                answer, usage = _call(
                    get_llm(), {**state, "usage": usage}, system=prompts.EXPLAIN_SYSTEM,
                    messages=messages, schema=prompts.EXPLAIN_SCHEMA,
                )
            except LLMError as exc:
                log.warning("Explanation skipped: %s", exc)
                break
            text, caveats = answer.get("explanation", ""), list(answer.get("caveats") or [])
            bad = numbers.unsupported_numbers(" ".join([text, *caveats]), facts)
            if not bad:
                break
            log.warning("Explanation quoted numbers not in the results: %s", bad)
            messages = messages + [
                {"role": "assistant", "content": json.dumps(answer)},
                {"role": "user", "content": (
                    f"These figures are not in the results: {', '.join(bad)}. Rewrite using only "
                    "figures that appear in the results, or describe them in words."
                )},
            ]
            text, caveats = "", []
        if not text:
            text = _plain_summary(run)
            caveats = []
        return {
            "phase": "awaiting_followup", "usage": usage,
            "transcript": [_event("assistant", "explanation", text, caveats=caveats)],
        }

    def followup_node(state: BuilderState) -> dict[str, Any]:
        reply = interrupt({"type": "followup"})
        text = str(reply.get("text", "") if isinstance(reply, dict) else reply).strip()
        return {
            "mode": "refine", "request": text, "phase": "compiling", "usage": _fresh_usage(),
            "transcript": [_event("user", "text", text)],
        }

    def refine_node(state: BuilderState) -> dict[str, Any]:
        current = dict(state.get("document") or {})
        current.pop("id", None)
        messages = [{"role": "user", "content": (
            "Current strategy document:\n" + json.dumps(current, sort_keys=True)
            + "\n\nRequested change (from the trader):\n" + state.get("request", "")
        )}]
        try:
            answer, usage = _call(
                get_llm(), state, system=prompts.refine_system(),
                messages=messages, schema=prompts.REFINE_SCHEMA,
            )
        except LLMError as exc:
            return _failure(exc)

        status = answer.get("status")
        if status == "unsupported":
            reason = answer.get("unsupported_reason") or "That change cannot be expressed."
            return {"usage": usage, "phase": "awaiting_approval", "candidate_text": "",
                    "transcript": [_event("assistant", "text", reason)]}
        if status == "needs_clarification":
            questions = (answer.get("questions") or [])[: max(0, prompts.MAX_QUESTIONS - state.get("questions_asked", 0))]
            if questions:
                return {"usage": usage, "questions": questions, "phase": "awaiting_answers",
                        "questions_asked": state.get("questions_asked", 0) + len(questions),
                        "dialogue": [{"role": "user", "content": messages[0]["content"]}],
                        "mode": "compile",
                        "transcript": [_event("assistant", "questions", "", questions=questions)]}
        try:
            patched = apply_patch(current, decode_ops(answer.get("patch") or []))
        except PatchError as exc:
            log.warning("Patch failed: %s", exc)
            return {"usage": usage, "candidate_text": "",
                    "transcript": [_event("assistant", "error", f"The change could not be applied: {exc}")],
                    "phase": "awaiting_approval"}
        assumptions = list(dict.fromkeys((state.get("assumptions") or []) + (answer.get("assumptions") or [])))
        return {
            "usage": usage, "candidate_text": json.dumps(patched), "assumptions": assumptions,
            "repairs": 0,
            "dialogue": [{"role": "user", "content": messages[0]["content"]}],
            "transcript": [_event("assistant", "change", answer.get("change_summary", ""))],
        }

    return {
        "compile": compile_node, "clarify": clarify_node, "validate": validate_node,
        "repair": repair_node, "readback": readback_node, "save": save_node,
        "backtest": backtest_node, "explain": explain_node, "followup": followup_node,
        "refine": refine_node,
    }


def _envelope(state: BuilderState, answer: dict, usage: dict, asked: int) -> dict[str, Any]:
    status = answer.get("status")
    assumptions = list(answer.get("assumptions") or [])
    if status == "unsupported":
        reason = answer.get("unsupported_reason") or "That cannot be expressed as a strategy here."
        return {"usage": usage, "phase": "unsupported",
                "transcript": [_event("assistant", "unsupported", reason)]}
    if status == "needs_clarification" and asked < prompts.MAX_QUESTIONS:
        questions = (answer.get("questions") or [])[: prompts.MAX_QUESTIONS - asked]
        if questions:
            dialogue = list(state.get("dialogue") or [])
            dialogue.append({"role": "assistant", "content": json.dumps({"questions": questions})})
            return {
                "usage": usage, "questions": questions, "questions_asked": asked + len(questions),
                "phase": "awaiting_answers", "dialogue": dialogue, "assumptions": assumptions,
                "transcript": [_event("assistant", "questions", "", questions=questions)],
            }
    text = answer.get("strategy_json") or ""
    return {
        "usage": usage, "candidate_text": text, "assumptions": assumptions, "repairs": 0,
        "errors": [] if text else [{"field": "", "message": "No document was returned"}],
    }


def _answers_text(questions: list[dict], reply: Any) -> str:
    if isinstance(reply, dict) and isinstance(reply.get("answers"), list):
        pairs = zip(questions, reply["answers"], strict=False)
        return "\n".join(f"Q: {q.get('question', '')}\nA: {a}" for q, a in pairs)
    if isinstance(reply, dict):
        return str(reply.get("text", ""))
    return str(reply)


def _normalize(document: dict[str, Any]) -> dict[str, Any]:
    """Resolve index aliases to keys and clear any model-chosen id."""
    from backend.data.instruments import INDICES

    document = dict(document)
    document["id"] = ""
    instrument = dict(document.get("instrument") or {})
    symbol = str(instrument.get("symbol", ""))
    alias = symbol.upper().replace(" ", "")
    if "|" not in symbol and alias in INDICES:
        instrument["symbol"] = INDICES[alias]
        document["instrument"] = instrument
    return document


def _run_summary(report: dict[str, Any]) -> dict[str, Any]:
    trades = report.get("trades") or []
    return {
        "run_id": report.get("run_id"),
        "strategy_hash": report.get("strategy_hash"),
        "symbol": report.get("symbol"),
        "timeframe": report.get("timeframe"),
        "synthetic_data": report.get("synthetic_data"),
        "metrics": report.get("metrics"),
        "split": report.get("split"),
        "warnings": report.get("warnings") or [],
        "blocked_reasons": report.get("blocked_reasons") or {},
        "data": report.get("data") or {},
        "trades": len(trades),
        "trials": report.get("trials"),
    }


def _explain_facts(state: BuilderState, run: dict[str, Any]) -> dict[str, Any]:
    data = run.get("data") or {}
    return {
        "strategy": readback.render(Strategy.model_validate(state["document"]))["summary"],
        "symbol": run.get("symbol"), "timeframe": run.get("timeframe"),
        "synthetic_data": run.get("synthetic_data"),
        "period": {"first_bar": data.get("first_bar"), "last_bar": data.get("last_bar"),
                   "bars": data.get("bars"), "missing_sessions": data.get("missing_sessions")},
        "trades": run.get("trades"), "metrics": run.get("metrics"), "split": run.get("split"),
        "risk_blocks": run.get("blocked_reasons"), "warnings": run.get("warnings"),
        "option_premiums": "model (Black-Scholes), not market prices"
        if Strategy.model_validate(state["document"]).has_options else None,
    }


def _plain_summary(run: dict[str, Any]) -> str:
    """Fallback explanation built from the numbers alone."""
    m = run.get("metrics") or {}
    parts = [f"{run.get('trades', 0)} trades"]
    for key, label in (("net_pnl", "net P&L"), ("max_drawdown_pct", "max drawdown %"),
                       ("win_rate", "win rate"), ("sharpe", "Sharpe")):
        if m.get(key) is not None:
            parts.append(f"{label} {m[key]}")
    text = "Backtest complete: " + ", ".join(parts) + "."
    if run.get("synthetic_data"):
        text += " This ran on synthetic data, so it says nothing about real markets."
    return text


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def _after_compile(state: BuilderState) -> str:
    phase = state.get("phase")
    if phase in ("failed", "unsupported"):
        return END
    if phase == "awaiting_answers":
        return "clarify"
    return "validate"


def _after_validate(state: BuilderState) -> str:
    if not state.get("errors"):
        return "readback"
    if state.get("repairs", 0) < MAX_REPAIRS:
        return "repair"
    # Out of repairs. A hand edit or a failed refinement goes back to the last
    # good document; a first compile has nothing to go back to.
    return "readback" if state.get("document") else "give_up"


def _after_repair(state: BuilderState) -> str:
    return END if state.get("phase") == "failed" else "validate"


def _after_readback(state: BuilderState) -> str:
    phase = state.get("phase")
    if phase == "backtesting":
        return "save"
    if phase == "rejected":
        return END
    # A hand edit carries the edited document; a change request carries none.
    return "validate" if state.get("candidate_text") else "refine"


def _after_refine(state: BuilderState) -> str:
    phase = state.get("phase")
    if phase == "failed":
        return END
    if phase == "awaiting_answers":
        return "clarify"
    if phase == "awaiting_approval":
        return "readback"
    return "validate"


def give_up_node(state: BuilderState) -> dict[str, Any]:
    errors = "; ".join(f"{e['field']}: {e['message']}" for e in state.get("errors") or [])
    text = (f"I could not produce a valid strategy after {MAX_REPAIRS} attempts. "
            f"Remaining problems: {errors}. Try describing it differently.")
    return {"phase": "failed", "error": text, "transcript": [_event("assistant", "error", text)]}


def build(
    get_llm: Callable[[], LLMClient],
    run_backtest: Callable[..., dict],
    save: Callable[..., tuple[str, int]],
    checkpointer=None,
):
    nodes = make_nodes(get_llm, run_backtest, save)
    g = StateGraph(BuilderState)
    for name, fn in nodes.items():
        g.add_node(name, fn)
    g.add_node("give_up", give_up_node)

    g.add_edge(START, "compile")
    g.add_conditional_edges("compile", _after_compile, ["clarify", "validate", END])
    g.add_edge("clarify", "compile")
    g.add_conditional_edges("validate", _after_validate, ["readback", "repair", "give_up"])
    g.add_conditional_edges("repair", _after_repair, ["validate", END])
    g.add_edge("give_up", END)
    g.add_conditional_edges("readback", _after_readback, ["save", "refine", "validate", END])
    g.add_edge("save", "backtest")
    g.add_edge("backtest", "explain")
    g.add_edge("explain", "followup")
    g.add_edge("followup", "refine")
    g.add_conditional_edges("refine", _after_refine, ["clarify", "readback", "validate", END])
    return g.compile(checkpointer=checkpointer)


def default_backtest_params(document: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    params = {"strategy": document, "include_chart": True}
    params.update({k: v for k, v in overrides.items() if v is not None})
    for key in ("start", "end"):
        value = params.get(key)
        if isinstance(value, date):
            params[key] = value.isoformat()
    return params
