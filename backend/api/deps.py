"""The API's one seam for identity.

Every router that touches per-user state takes `ctx: AppContext =
Depends(current_context)`. Today that is always the single local user; a hosted
version replaces this one function with a login check and every router follows
(plan §1, "Scope", rule 3).
"""

from __future__ import annotations

from backend.app.context import AppContext


def current_context() -> AppContext:
    return AppContext.local()
