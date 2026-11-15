"""Backups of what cannot be regenerated: strategies, runs, conversations, journals.

Market data is excluded on purpose: it is large and the cache re-downloads it.
Logs and credentials are excluded too.

SQLite files are copied with SQLite's online backup API, so a backup taken
while the app is running is a consistent snapshot, not a torn copy of a file
mid-write. Each archive carries a manifest with SHA-256 hashes, `verify`
checks them, and `restore` refuses to overwrite existing state unless told to.
A backup that has never been restored is a hope, so the round trip is tested.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from backend.core.config import IST, settings
from backend.core.version import __version__

log = logging.getLogger(__name__)

KEEP = 14
SQLITE_FILES = ("app.sqlite", "agent_checkpoints.sqlite")
MANIFEST = "manifest.json"


class BackupError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot(source: Path, target: Path) -> None:
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def list_backups() -> list[Path]:
    directory = settings.backups_dir
    return sorted(directory.glob("backup-*.zip")) if directory.exists() else []


def latest() -> Path | None:
    backups = list_backups()
    return backups[-1] if backups else None


def create(now: datetime | None = None) -> Path | None:
    """Write a backup archive. Returns None when there is nothing to back up."""
    now = now or datetime.now(IST)
    data = settings.data_dir
    sqlite_sources = [data / name for name in SQLITE_FILES if (data / name).exists()]
    journals = sorted(settings.journal_dir.glob("*.jsonl")) if settings.journal_dir.exists() else []
    if not sqlite_sources and not journals:
        return None

    settings.backups_dir.mkdir(parents=True, exist_ok=True)
    target = settings.backups_dir / f"backup-{now:%Y%m%d-%H%M%S}.zip"
    partial = target.with_suffix(".zip.partial")
    files: dict[str, dict[str, Any]] = {}

    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as zf:
        for source in sqlite_sources:
            copy = Path(tmp) / source.name
            _snapshot(source, copy)
            zf.write(copy, source.name)
            files[source.name] = {"sha256": _sha256(copy), "bytes": copy.stat().st_size}
        for journal in journals:
            name = f"journal/{journal.name}"
            zf.write(journal, name)
            files[name] = {"sha256": _sha256(journal), "bytes": journal.stat().st_size}
        zf.writestr(MANIFEST, json.dumps(
            {"app_version": __version__, "created_at": now.isoformat(timespec="seconds"), "files": files},
            indent=1,
        ))
    partial.chmod(0o600)
    partial.replace(target)
    log.info("Backup written: %s (%d file(s))", target.name, len(files))
    _prune()
    return target


def _prune(keep: int = KEEP) -> None:
    for old in list_backups()[:-keep]:
        old.unlink(missing_ok=True)
        log.info("Pruned old backup %s", old.name)


def due(now: datetime | None = None, every: timedelta = timedelta(hours=24)) -> bool:
    newest = latest()
    if newest is None:
        return True
    now = now or datetime.now(IST)
    age = now.timestamp() - newest.stat().st_mtime
    return age >= every.total_seconds()


def create_if_due() -> Path | None:
    """Daily backup, called at startup. Never raises: a failed backup must not
    stop the app, but it is logged loudly and `doctor` reports the age."""
    try:
        return create() if due() else None
    except Exception:
        log.exception("Backup failed")
        return None


def verify(archive: Path) -> dict[str, Any]:
    """Check every file against the manifest. Raises BackupError on any mismatch."""
    try:
        with zipfile.ZipFile(archive) as zf:
            manifest = json.loads(zf.read(MANIFEST))
            for name, meta in manifest["files"].items():
                digest = hashlib.sha256(zf.read(name)).hexdigest()
                if digest != meta["sha256"]:
                    raise BackupError(f"{archive.name}: {name} does not match its recorded hash")
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError) as exc:
        raise BackupError(f"{archive.name} is not a readable backup: {exc}") from exc
    return manifest


def restore(archive: Path, force: bool = False) -> list[str]:
    """Put a backup's files back into the data directory.

    Stop the app first. Existing files are never overwritten unless `force`.
    """
    manifest = verify(archive)
    data = settings.data_dir
    targets = {name: data / name for name in manifest["files"]}
    existing = [name for name, path in targets.items() if path.exists()]
    if existing and not force:
        raise BackupError(
            "These files already exist and would be overwritten: "
            + ", ".join(existing[:5]) + (" ..." if len(existing) > 5 else "")
            + ". Move them away, or pass --force."
        )
    with zipfile.ZipFile(archive) as zf:
        for name, path in targets.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            partial = path.with_name(path.name + ".restoring")
            partial.write_bytes(zf.read(name))
            partial.chmod(0o600)
            partial.replace(path)
            # A stale write-ahead log must not be replayed onto the restored file.
            for suffix in ("-wal", "-shm"):
                path.with_name(path.name + suffix).unlink(missing_ok=True)
    return sorted(targets)
