"""Terminal UI. All mutating commands display the scope or require explicit approval."""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys

from .engine import apply, inspect_history, rollback
from .planner import PROJECT, build_plan
from .windows import SafetyError

def parser():
    cli = argparse.ArgumentParser(description="Preview and organize local Windows files.")
    commands = cli.add_subparsers(dest="command", required=True)
    for name, help_text in (("plan", "inspect files and preview proposed moves"),
                            ("apply", "review, approve and execute moves"),
                            ("history", "list runs or show one run"),
                            ("rollback", "review and reverse a recorded run")):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("folder", type=Path, help="selected local data folder")
        sub.add_argument("--json", action="store_true", help="machine-readable stdout")
        if name in {"plan", "apply"}:
            sub.add_argument("--recursive", action=argparse.BooleanOptionalAction, default=None,
                             help="include subfolders (overrides settings)")
            sub.add_argument("--config", default=PROJECT / "settings.json", type=Path)
        if name == "apply":
            mode = sub.add_mutually_exclusive_group()
            mode.add_argument("--approve", metavar="PLAN_ID", help="execute this exact preview")
            mode.add_argument("--dry-run", action="store_true", help="preview only")
        if name == "history":
            sub.add_argument("run_id", nargs="?")
        if name == "rollback":
            sub.add_argument("run_id")
            mode = sub.add_mutually_exclusive_group()
            mode.add_argument("--yes", action="store_true", help="approve this run's rollback")
            mode.add_argument("--dry-run", action="store_true", help="list recorded rollback candidates")
    return cli

def emit(data):
    print(json.dumps(data, ensure_ascii=True, indent=2))

def show_plan(plan):
    print(f"Folder  {plan.root}")
    for item in plan.items:
        line = f"{item.status.upper():7} {item.source}"
        if item.target:
            line += f" -> {item.target}"
        if item.snapshot:
            snap = item.snapshot
            try:
                stamp = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=snap["mtime_ns"] // 10**9)
                when = stamp.isoformat(timespec="seconds")
            except OverflowError:
                when = f"mtime_ns={snap['mtime_ns']}"
            line += f"  [{snap['size']} bytes, {when}]"
        print(f"{line}  | {item.reason}")
    for group in plan.duplicates:
        print("DUPLICATE NAME  " + " | ".join(group))
    print(f"{len(plan.moves)} to move, {sum(i.status == 'review' for i in plan.items)} for review,"
          f" {sum(i.status == 'skip' for i in plan.items)} skipped")
    print(f"Plan ID  {plan.token}")

def show_run(report):
    print(f"Run  {report['id']}  {report['started']}")
    for row in report["moves"]:
        print(f"{row['state'].upper():9} {row['src']} <-> {row['dst']}  {row['detail']}")
    print("  ".join(f"{v} {k}" for k, v in Counter(r["state"] for r in report["moves"]).items()))

def confirm(expected):
    if not sys.stdin.isatty():
        raise SafetyError("Interactive approval requires a terminal. Use the explicit approval option.")
    try:
        return input(f"Type {expected} to continue, or press Enter to cancel: ").strip() == expected
    except EOFError:
        return False

def main(argv=None):
    args = parser().parse_args(argv)
    # Redirected output may use a legacy Windows code page. Escape names there
    # instead of terminating a run just because a filename cannot be printed.
    for output in (sys.stdout, sys.stderr):
        if hasattr(output, "reconfigure"):
            output.reconfigure(encoding="utf-8", errors="backslashreplace")
    try:
        if args.command in {"plan", "apply"}:
            plan = build_plan(args.folder, args.config, args.recursive)
            data = plan.payload() | {"plan_id": plan.token}
            if not args.json:
                show_plan(plan)
            if args.command == "plan" or args.dry_run:
                if args.json:
                    emit(data)
                return 0
            approval = args.approve
            if approval is None and plan.moves:
                if args.json:
                    raise SafetyError("Use --approve PLAN_ID with --json.")
                if not confirm(plan.token):
                    print("Cancelled.")
                    return 1
                approval = plan.token
            run = apply(plan, plan.token if approval is None else approval)
            if args.json:
                emit({"plan": data, "run": run})
            elif run:
                show_run(run)
            return 2 if (any(i.status == "review" for i in plan.items) or
                         (run and any(r["state"] != "moved" for r in run["moves"]))) else 0

        if args.command == "history":
            data = inspect_history(args.folder, args.run_id)
            if args.json:
                emit(data)
            elif args.run_id:
                show_run(data)
            elif data:
                for run in data:
                    print(f"{run['id']}  {run['started']}  {run['counts']}")
            else:
                print("No recorded runs.")
            return 0

        preview = inspect_history(args.folder, args.run_id)
        if not args.json:
            show_run(preview)
        if args.dry_run:
            if args.json:
                emit(preview)
            return 0
        if not args.yes:
            if args.json:
                raise SafetyError("Use --yes to approve rollback with --json.")
            if not confirm("ROLLBACK " + args.run_id):
                print("Cancelled.")
                return 1
        run = rollback(args.folder, args.run_id)
        if args.json:
            emit(run)
        else:
            show_run(run)
        return 2 if any(r["state"] not in {"restored", "skipped"} for r in run["moves"]) else 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted. Inspect history for the selected folder.", file=sys.stderr)
        return 130
