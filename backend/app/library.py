"""The strategy library: saved strategies and their full version history.

A version is identified by the strategy's content hash, which covers everything
that affects behaviour and nothing else (not the name or description). So:

- saving an edit that changes behaviour adds a version, linked to its parent;
- saving with only a new name or description updates the current version in place;
- saving a document identical to an earlier version moves the head back to it
  rather than duplicating it, which is what "undo" looks like in history.

Shipped templates are imported as read-only entries. Editing one means forking
it into a strategy of your own, so a template can never drift from the code
that defines it.

Every strategy belongs to a family (ideas derived from one another). Backtests
are counted per family by the run store, which is what makes a multiple-testing
correction possible later.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from backend.app.context import AppContext
from backend.app.db import get_app_db, now_iso
from backend.strategy.schema import Strategy

Source = Literal["llm", "builder", "template", "import"]


class LibraryError(Exception):
    """A request the library refuses (unknown id, read-only entry, ...)."""


class NotFound(LibraryError):
    pass


class ReadOnly(LibraryError):
    pass


@dataclass(frozen=True)
class Provenance:
    """Where a version came from, when the language model produced it."""

    model: str | None = None
    prompt_version: str | None = None
    conversation_id: str | None = None


@dataclass(frozen=True)
class StrategyRecord:
    id: str
    family_id: str
    name: str
    source: str
    head_hash: str
    read_only: bool
    archived: bool
    created_at: str
    updated_at: str
    version_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class VersionRecord:
    strategy_id: str
    hash: str
    parent_hash: str | None
    schema_version: int
    source: str
    model: str | None
    prompt_version: str | None
    conversation_id: str | None
    note: str | None
    created_at: str
    document: dict[str, Any]

    def strategy(self) -> Strategy:
        return Strategy.model_validate(self.document)

    def to_dict(self, include_document: bool = True) -> dict[str, Any]:
        out = dict(self.__dict__)
        if not include_document:
            out.pop("document")
        return out


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class StrategyLibrary:
    def __init__(self, ctx: AppContext | None = None) -> None:
        self.ctx = ctx or AppContext.local()
        self.db = get_app_db(self.ctx)

    # -- reads ----------------------------------------------------------------

    def get(self, strategy_id: str) -> StrategyRecord:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT s.*, (SELECT count(*) FROM strategy_versions v WHERE v.strategy_id = s.id)"
                " AS version_count FROM strategies s WHERE s.id = ? AND s.user_id = ?",
                (strategy_id, self.ctx.user_id),
            ).fetchone()
        if row is None:
            raise NotFound(f"No strategy {strategy_id!r}")
        return _strategy_record(row)

    def list_strategies(
        self, include_archived: bool = False, include_templates: bool = True
    ) -> list[StrategyRecord]:
        query = (
            "SELECT s.*, (SELECT count(*) FROM strategy_versions v WHERE v.strategy_id = s.id)"
            " AS version_count FROM strategies s WHERE s.user_id = ?"
        )
        params: list[Any] = [self.ctx.user_id]
        if not include_archived:
            query += " AND s.archived = 0"
        if not include_templates:
            query += " AND s.source != 'template'"
        query += " ORDER BY s.read_only, s.updated_at DESC"
        with self.db.read() as conn:
            return [_strategy_record(r) for r in conn.execute(query, params)]

    def versions(self, strategy_id: str) -> list[VersionRecord]:
        """Every version, newest first."""
        self.get(strategy_id)
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM strategy_versions WHERE strategy_id = ? AND user_id = ?"
                " ORDER BY created_at DESC, rowid DESC",
                (strategy_id, self.ctx.user_id),
            ).fetchall()
        return [_version_record(r) for r in rows]

    def version(self, strategy_id: str, content_hash: str | None = None) -> VersionRecord:
        """One version; the current head when `content_hash` is omitted."""
        record = self.get(strategy_id)
        wanted = content_hash or record.head_hash
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM strategy_versions WHERE strategy_id = ? AND hash = ? AND user_id = ?",
                (strategy_id, wanted, self.ctx.user_id),
            ).fetchone()
        if row is None:
            raise NotFound(f"Strategy {strategy_id!r} has no version {wanted[:12]}")
        return _version_record(row)

    # -- writes ---------------------------------------------------------------

    def create(
        self,
        strategy: Strategy,
        source: Source,
        *,
        name: str | None = None,
        family_id: str | None = None,
        provenance: Provenance | None = None,
        note: str | None = None,
        strategy_id: str | None = None,
        read_only: bool = False,
    ) -> StrategyRecord:
        """Save a new strategy with its first version."""
        if name and name != strategy.name:
            # The library name and the document's name are kept identical.
            strategy = strategy.model_copy(update={"name": name})
        sid = strategy_id or _new_id("lib")
        family = family_id or sid
        content_hash = strategy.content_hash()
        stamp = now_iso()
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO strategies (id, user_id, family_id, name, source, head_hash,"
                " read_only, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, self.ctx.user_id, family, strategy.name, source, content_hash,
                 int(read_only), stamp, stamp),
            )
            self._insert_version(conn, sid, strategy, source, None, provenance, note, stamp)
            self._adopt_unsaved_runs(conn, sid, family, content_hash)
        return self.get(sid)

    def save_version(
        self,
        strategy_id: str,
        strategy: Strategy,
        source: Source,
        *,
        provenance: Provenance | None = None,
        note: str | None = None,
    ) -> VersionRecord:
        """Record `strategy` as the new head of `strategy_id`. See the module docstring
        for what happens when nothing, or only the name, changed."""
        record = self.get(strategy_id)
        if record.read_only:
            raise ReadOnly(f"{record.name!r} is a read-only template. Fork it to edit.")
        content_hash = strategy.content_hash()
        stamp = now_iso()
        document = json.dumps(strategy.model_dump(mode="json"), sort_keys=True)
        with self.db.connect() as conn:
            existing = conn.execute(
                "SELECT 1 FROM strategy_versions WHERE strategy_id = ? AND hash = ?",
                (strategy_id, content_hash),
            ).fetchone()
            if existing:
                # Same behaviour as a version we already have: refresh its document
                # (name, description) and point the head at it.
                conn.execute(
                    "UPDATE strategy_versions SET spec_json = ? WHERE strategy_id = ? AND hash = ?",
                    (document, strategy_id, content_hash),
                )
            else:
                self._insert_version(
                    conn, strategy_id, strategy, source, record.head_hash, provenance, note, stamp
                )
                self._adopt_unsaved_runs(conn, strategy_id, record.family_id, content_hash)
            conn.execute(
                "UPDATE strategies SET head_hash = ?, name = ?, updated_at = ? WHERE id = ?",
                (content_hash, strategy.name, stamp, strategy_id),
            )
        return self.version(strategy_id, content_hash)

    def fork(self, strategy_id: str, name: str | None = None) -> StrategyRecord:
        """Copy the head of `strategy_id` into a new, editable strategy.

        A fork of a template starts a new family; a fork of your own strategy
        stays in its family, since it is a variation of the same idea.
        """
        record = self.get(strategy_id)
        head = self.version(strategy_id)
        strategy = head.strategy()
        new_name = name or f"{record.name} (copy)"
        strategy = strategy.model_copy(update={"name": new_name})
        family = None if record.source == "template" else record.family_id
        source: Source = "builder" if record.source == "template" else record.source  # type: ignore[assignment]
        return self.create(strategy, source, name=new_name, family_id=family,
                           note=f"forked from {record.name} @ {head.hash[:12]}")

    def rename(self, strategy_id: str, name: str) -> StrategyRecord:
        name = name.strip()
        if not name:
            raise LibraryError("A strategy needs a name")
        record = self.get(strategy_id)
        if record.read_only:
            raise ReadOnly(f"{record.name!r} is a read-only template. Fork it to rename.")
        head = self.version(strategy_id)
        self.save_version(strategy_id, head.strategy().model_copy(update={"name": name}), head.source)  # type: ignore[arg-type]
        return self.get(strategy_id)

    def set_archived(self, strategy_id: str, archived: bool) -> StrategyRecord:
        self.get(strategy_id)
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE strategies SET archived = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                (int(archived), now_iso(), strategy_id, self.ctx.user_id),
            )
        return self.get(strategy_id)

    def delete(self, strategy_id: str) -> None:
        """Delete a strategy and its versions. Its runs stay, keyed by content hash."""
        record = self.get(strategy_id)
        if record.read_only:
            raise ReadOnly("Templates can't be deleted; archive them instead.")
        with self.db.connect() as conn:
            conn.execute("DELETE FROM strategies WHERE id = ? AND user_id = ?",
                         (strategy_id, self.ctx.user_id))

    def import_templates(self) -> int:
        """Make sure every shipped template is in the library. Returns how many
        were added or updated. Idempotent; a template whose code changed gets a
        new version rather than a silent overwrite."""
        from backend.strategy import templates

        changed = 0
        for key in templates.TEMPLATES:
            strategy = templates.load(key)
            sid = f"tpl_{key}"
            try:
                record = self.get(sid)
            except NotFound:
                self.create(strategy, "template", strategy_id=sid, read_only=True)
                changed += 1
                continue
            if record.head_hash != strategy.content_hash():
                self._update_template(sid, strategy, record.head_hash)
                changed += 1
        return changed

    # -- internals ------------------------------------------------------------

    def _update_template(self, sid: str, strategy: Strategy, parent: str) -> None:
        stamp = now_iso()
        with self.db.connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM strategy_versions WHERE strategy_id = ? AND hash = ?",
                (sid, strategy.content_hash()),
            ).fetchone()
            if not exists:
                self._insert_version(conn, sid, strategy, "template", parent, None,
                                     "template updated", stamp)
            conn.execute(
                "UPDATE strategies SET head_hash = ?, name = ?, updated_at = ? WHERE id = ?",
                (strategy.content_hash(), strategy.name, stamp, sid),
            )

    def _insert_version(self, conn, sid, strategy: Strategy, source, parent, provenance, note, stamp) -> None:
        provenance = provenance or Provenance()
        conn.execute(
            "INSERT INTO strategy_versions (strategy_id, hash, user_id, parent_hash, schema_version,"
            " spec_json, source, model, prompt_version, conversation_id, note, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, strategy.content_hash(), self.ctx.user_id, parent, strategy.schema_version,
             json.dumps(strategy.model_dump(mode="json"), sort_keys=True), source,
             provenance.model, provenance.prompt_version, provenance.conversation_id, note, stamp),
        )

    def _adopt_unsaved_runs(self, conn, sid: str, family: str, content_hash: str) -> None:
        """Backtests run before the strategy was saved now belong to it, so the
        trial count of its family includes them."""
        conn.execute(
            "UPDATE runs SET strategy_id = ?, family_id = ? "
            "WHERE user_id = ? AND strategy_id IS NULL AND strategy_hash = ?",
            (sid, family, self.ctx.user_id, content_hash),
        )


def _strategy_record(row) -> StrategyRecord:
    return StrategyRecord(
        id=row["id"], family_id=row["family_id"], name=row["name"], source=row["source"],
        head_hash=row["head_hash"], read_only=bool(row["read_only"]),
        archived=bool(row["archived"]), created_at=row["created_at"],
        updated_at=row["updated_at"], version_count=int(row["version_count"]),
    )


def _version_record(row) -> VersionRecord:
    return VersionRecord(
        strategy_id=row["strategy_id"], hash=row["hash"], parent_hash=row["parent_hash"],
        schema_version=row["schema_version"], source=row["source"], model=row["model"],
        prompt_version=row["prompt_version"], conversation_id=row["conversation_id"],
        note=row["note"], created_at=row["created_at"], document=json.loads(row["spec_json"]),
    )
