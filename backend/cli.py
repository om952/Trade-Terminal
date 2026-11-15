"""The `trade-terminal` command.

    trade-terminal                 start the app and open it in the browser
    trade-terminal serve --port N  the same, on another port
    trade-terminal doctor          check the installation
    trade-terminal backup          back up strategies, runs and journals now
    trade-terminal restore FILE    restore a backup (stop the app first)
    trade-terminal diagnostics     write a bundle that is safe to share
    trade-terminal sync-instruments  refresh the instrument master
    trade-terminal where           show where data and settings live

The server always binds to 127.0.0.1. There is no flag to expose it: the API
has no login, and serving it to a network is the remote-deployment work in
PRODUCTION_PLAN.md P5.6.
"""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser
from pathlib import Path

HOST = "127.0.0.1"


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from backend.ops.doctor import ui_dir

    url = f"http://{HOST}:{args.port}"
    if ui_dir() is None:
        print("No built UI found. The API will run; for the UI run the Vite dev server "
              "(cd frontend && npm run dev) or build it (npm run build).")
    elif not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"Trade Terminal on {url}  (Ctrl+C to stop)")
    uvicorn.run("backend.api.main:app", host=HOST, port=args.port, log_level="warning")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from backend.ops import doctor

    checks = doctor.run()
    print(doctor.render(checks))
    return doctor.exit_code(checks)


def cmd_backup(args: argparse.Namespace) -> int:
    from backend.ops import backup

    path = backup.create()
    if path is None:
        print("Nothing to back up yet.")
        return 0
    backup.verify(path)
    print(f"Backup written and verified: {path}")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    from backend.ops import backup

    try:
        restored = backup.restore(Path(args.file), force=args.force)
    except backup.BackupError as exc:
        print(f"Not restored: {exc}")
        return 1
    print(f"Restored {len(restored)} file(s) into the data directory.")
    return 0


def cmd_diagnostics(args: argparse.Namespace) -> int:
    from backend.ops import diagnostics

    path = diagnostics.create()
    print(f"Diagnostics bundle: {path}\n"
          f"It holds versions, health checks and a redacted log tail; no strategies, "
          f"trades or credentials.")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    from backend.data import instruments

    count = instruments.sync("complete")
    print(f"{count:,} instruments synced.")
    return 0


def cmd_where(args: argparse.Namespace) -> int:
    from backend.core.config import HOME, IS_CHECKOUT, settings

    print(f"home      {HOME}  ({'source checkout' if IS_CHECKOUT else 'installed'})")
    print(f"settings  {HOME / '.env'}")
    print(f"data      {settings.data_dir}")
    print(f"logs      {settings.logs_dir}")
    print(f"backups   {settings.backups_dir}")
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    from backend.core.version import __version__

    print(__version__)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="trade-terminal", description="Trade Terminal")
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="start the app (the default)")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--no-browser", action="store_true")
    serve.set_defaults(func=cmd_serve)

    sub.add_parser("doctor", help="check the installation").set_defaults(func=cmd_doctor)
    sub.add_parser("backup", help="back up strategies, runs and journals").set_defaults(func=cmd_backup)
    restore = sub.add_parser("restore", help="restore a backup (stop the app first)")
    restore.add_argument("file")
    restore.add_argument("--force", action="store_true", help="overwrite existing files")
    restore.set_defaults(func=cmd_restore)
    sub.add_parser("diagnostics", help="write a shareable diagnostics bundle").set_defaults(func=cmd_diagnostics)
    sub.add_parser("sync-instruments", help="refresh the instrument master").set_defaults(func=cmd_sync)
    sub.add_parser("where", help="show where data and settings live").set_defaults(func=cmd_where)
    sub.add_parser("version", help="print the version").set_defaults(func=cmd_version)

    args = parser.parse_args(argv)
    if args.version:
        return cmd_version(args)
    if not args.command:
        args = parser.parse_args(["serve", *(argv or [])])

    if args.command != "serve":
        from backend.core.logsetup import configure_logging

        configure_logging(to_file=False)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
