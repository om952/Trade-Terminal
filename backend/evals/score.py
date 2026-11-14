"""Score one compile against its gold case, field by field.

A case's gold document says only what the prompt says. Fields the prompt leaves
open are not scored: `session` and each `risk` key are compared only when the
gold document sets them, and a case may list further fields in `skip`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.evals.canonical import canonical
from backend.strategy.schema import Strategy

FIELDS = ("instrument", "timeframe", "entry", "exit", "legs", "risk", "session")


@dataclass
class Outcome:
    """What the builder did with one prompt."""

    status: str                               # ready | needs_clarification | unsupported | failed
    document: dict[str, Any] | None = None
    questions: list[dict[str, Any]] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    llm_calls: int = 0
    repairs: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    seconds: float = 0.0
    error: str | None = None
    model: str = ""


@dataclass
class Score:
    case_id: str
    expected_status: str
    status: str
    status_ok: bool
    valid: bool                               # a valid document came out (ready cases)
    fields: dict[str, bool] = field(default_factory=dict)
    exact: bool = False                       # status right and every scored field right
    mismatches: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id, "expected_status": self.expected_status,
            "status": self.status, "status_ok": self.status_ok, "valid": self.valid,
            "fields": self.fields, "exact": self.exact, "mismatches": self.mismatches,
        }


def gold_strategy(case: dict[str, Any]) -> Strategy:
    document = dict(case["expect"]["document"])
    document.setdefault("name", case["id"])
    return Strategy.model_validate(document)


def scored_fields(case: dict[str, Any]) -> list[str]:
    gold = case["expect"]["document"]
    skip = set(case.get("skip", []))
    out = [f for f in ("instrument", "timeframe", "entry", "exit", "legs") if f not in skip]
    if gold.get("risk") and "risk" not in skip:
        out.append("risk")
    if "session" in gold and "session" not in skip:
        out.append("session")
    return out


def score(case: dict[str, Any], outcome: Outcome) -> Score:
    expected = case["expect"]["status"]
    result = Score(
        case_id=case["id"], expected_status=expected, status=outcome.status,
        status_ok=outcome.status == expected, valid=False,
    )
    if expected != "ready":
        result.exact = result.status_ok
        return result
    if outcome.status != "ready" or not outcome.document:
        return result

    try:
        actual = canonical(Strategy.model_validate(outcome.document))
    except Exception as exc:
        result.mismatches["document"] = f"invalid: {exc}"
        return result
    result.valid = True
    gold_doc = case["expect"]["document"]
    gold = canonical(gold_strategy(case))

    for name in scored_fields(case):
        if name == "risk":
            keys = _flat_keys(gold_doc["risk"])
            want = {k: gold["risk"][k] for k in keys}
            got = {k: actual["risk"][k] for k in keys}
        elif name == "session":
            keys = list(gold_doc["session"]) or ["intraday"]
            keys = ["intraday", *[k for k in keys if k != "intraday"]]
            want = {k: gold["session"][k] for k in keys}
            got = {k: actual["session"][k] for k in keys}
        else:
            want, got = gold[name], actual[name]
        ok = want == got
        result.fields[name] = ok
        if not ok:
            result.mismatches[name] = {"expected": _show(want), "actual": _show(got)}

    result.exact = result.status_ok and all(result.fields.values())
    return result


def _flat_keys(risk: dict[str, Any]) -> list[str]:
    out = []
    for key, value in risk.items():
        if key == "re_entry" and isinstance(value, dict):
            out.extend(f"re_entry.{k}" for k in value)
        else:
            out.append(key)
    return out


def _show(value: Any) -> Any:
    return value if isinstance(value, (dict, str, int, float, type(None))) else repr(value)
