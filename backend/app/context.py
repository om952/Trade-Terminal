"""Who the current user is, and where their files live.

The local app has exactly one user, `"local"`. Code that needs per-user paths or
identity takes an `AppContext` instead of reaching for module globals, so a
hosted, multi-user version can hand each request its own context without the
callers changing (plan §1, "Scope", rule 2).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from backend.core.config import settings

LOCAL_USER = "local"


@dataclass(frozen=True)
class AppContext:
    user_id: str
    data_dir: Path

    @property
    def app_db_path(self) -> Path:
        return self.data_dir / "app.sqlite"

    @classmethod
    def local(cls) -> AppContext:
        """The single user of the local app. Reads `settings` at call time, so a
        test that points `settings.data_dir` elsewhere gets a matching context."""
        return cls(user_id=LOCAL_USER, data_dir=settings.data_dir)
