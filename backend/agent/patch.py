"""Apply a JSON Patch (RFC 6902 add / replace / remove) to a strategy document.

A follow-up such as "tighten the stop to 1%" becomes a small patch, not a whole
new document, so an unrelated part of the strategy cannot drift between
versions because the model regenerated it slightly differently.
"""

from __future__ import annotations

import copy
import json
from typing import Any


class PatchError(ValueError):
    pass


def _tokens(path: str) -> list[str]:
    if path == "":
        return []
    if not path.startswith("/"):
        raise PatchError(f"Path {path!r} must start with '/'")
    return [t.replace("~1", "/").replace("~0", "~") for t in path[1:].split("/")]


def _index(container: list, token: str, allow_end: bool) -> int:
    if token == "-" and allow_end:
        return len(container)
    if not token.isdigit():
        raise PatchError(f"{token!r} is not a list index")
    i = int(token)
    if i > len(container) or (i == len(container) and not allow_end):
        raise PatchError(f"Index {i} is out of range (length {len(container)})")
    return i


def apply_patch(document: dict[str, Any], ops: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a patched copy. Each op: {"op", "path", "value"} (value omitted for remove)."""
    doc = copy.deepcopy(document)
    for n, op in enumerate(ops, 1):
        kind, path = op.get("op"), op.get("path", "")
        tokens = _tokens(path)
        if not tokens:
            raise PatchError(f"Op {n}: cannot {kind} the whole document")
        parent: Any = doc
        for token in tokens[:-1]:
            if isinstance(parent, list):
                parent = parent[_index(parent, token, allow_end=False)]
            elif isinstance(parent, dict) and token in parent:
                parent = parent[token]
            else:
                raise PatchError(f"Op {n}: {path!r} does not exist")
        last = tokens[-1]
        try:
            if kind == "add":
                if isinstance(parent, list):
                    parent.insert(_index(parent, last, allow_end=True), op["value"])
                else:
                    parent[last] = op["value"]
            elif kind == "replace":
                if isinstance(parent, list):
                    parent[_index(parent, last, allow_end=False)] = op["value"]
                elif last in parent:
                    parent[last] = op["value"]
                else:
                    raise PatchError(f"Op {n}: cannot replace missing {path!r}")
            elif kind == "remove":
                if isinstance(parent, list):
                    parent.pop(_index(parent, last, allow_end=False))
                elif last in parent:
                    del parent[last]
                else:
                    raise PatchError(f"Op {n}: cannot remove missing {path!r}")
            else:
                raise PatchError(f"Op {n}: unsupported op {kind!r} (use add, replace, remove)")
        except KeyError as exc:
            raise PatchError(f"Op {n}: missing field {exc}") from exc
        except TypeError as exc:
            raise PatchError(f"Op {n}: {path!r} is not a container") from exc
    return doc


def decode_ops(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Model ops carry values as JSON text (`value_json`); decode them."""
    out = []
    for n, op in enumerate(raw, 1):
        decoded = {"op": op.get("op"), "path": op.get("path", "")}
        if op.get("op") != "remove":
            try:
                decoded["value"] = json.loads(op.get("value_json", ""))
            except json.JSONDecodeError as exc:
                raise PatchError(f"Op {n}: value_json is not valid JSON ({exc})") from exc
        out.append(decoded)
    return out
