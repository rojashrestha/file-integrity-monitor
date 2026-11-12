"""Command-line interface for the File Integrity Monitor."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from .monitor import (
    MonitorError, check, has_changes, make_baseline,
    resolved_baseline, resolved_root, write_baseline,
)

DEFAULT_BASELINE = ".fim/baseline.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fim", description="Detect file additions, deletions and content changes with SHA-256."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common_options(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument("directory", help="Directory you want to monitor")
        command_parser.add_argument(
            "--baseline", default=DEFAULT_BASELINE,
            help=f"Baseline JSON path (default: {DEFAULT_BASELINE}); must be outside monitored directory"
        )

    initialize = sub.add_parser("init", help="Create a trusted initial snapshot")
    common_options(initialize)
    initialize.add_argument(
        "--force", action="store_true", help="Replace an existing baseline after reviewing changes"
    )

    verify = sub.add_parser("check", help="Compare files against the saved snapshot")
    common_options(verify)
    verify.add_argument("--json", action="store_true", help="Print machine-readable JSON report")

    watch = sub.add_parser("watch", help="Keep checking until Ctrl+C is pressed")
    common_options(watch)
    watch.add_argument(
        "--interval", type=float, default=5.0, metavar="SECONDS",
        help="Seconds between checks (default: 5)"
    )
    return parser


def show_report(report: dict) -> None:
    markers = (("added", "NEW"), ("modified", "MODIFIED"), ("deleted", "DELETED"))
    for key, label in markers:
        for path in report[key]:
            print(f"[{label}] {path}")
    if not has_changes(report):
        print("[OK] No changes detected.")
    print(
        f"Summary: {len(report['added'])} added, {len(report['modified'])} modified, "
        f"{len(report['deleted'])} deleted, {report['unchanged_count']} unchanged "
        f"({report['scanned_count']} files currently scanned)."
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = resolved_root(args.directory)
        baseline_path = resolved_baseline(args.baseline, root)
        if args.command == "init":
            if baseline_path.exists() and not args.force:
                raise MonitorError(
                    f"Baseline already exists: {baseline_path}. "
                    "Review changes before replacing it with 'init --force'."
                )
            snapshot = make_baseline(root)
            write_baseline(snapshot, baseline_path, force=args.force)
            print(f"[OK] Baseline saved: {baseline_path}")
            print(f"Tracked {len(snapshot['files'])} files under {root}")
            return 0
        if args.command == "check":
            report = check(root, baseline_path)
            if args.json:
                print(json.dumps(report, indent=2))
            else:
                show_report(report)
            return 1 if has_changes(report) else 0
        if args.command == "watch":
            if args.interval <= 0:
                raise MonitorError("--interval must be greater than zero")
            # Validate baseline before starting the loop.
            from .monitor import load_baseline
            load_baseline(baseline_path, root)
            print(f"Watching {root} every {args.interval:g} seconds. Ctrl+C to stop.", flush=True)
            try:
                while True:
                    print(f"\n[{datetime.now().astimezone().isoformat(timespec='seconds')}]", flush=True)
                    show_report(check(root, baseline_path))
                    sys.stdout.flush()
                    time.sleep(args.interval)
            except KeyboardInterrupt:
                print("\nStopped monitoring.")
                return 0
    except MonitorError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
