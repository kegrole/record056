"""Command-line interface for applying CDC JSON Lines events."""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional, Sequence

from .apply import CDCApplier
from .events import parse_jsonl
from .store import CDCStore, PHYSICAL_DELETE, SOFT_DELETE, VALID_DELETE_MODES


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Apply JSON Lines CDC events to a SQLite table.")
    parser.add_argument("--events", required=True, help="Path to the JSON Lines event file")
    parser.add_argument("--db", required=True, help="Path to the SQLite database file")
    parser.add_argument("--out", required=True, help="Path to the JSON report output")
    parser.add_argument(
        "--delete-mode",
        choices=sorted(VALID_DELETE_MODES),
        default=SOFT_DELETE,
        help="Delete handling mode (default: soft)",
    )
    return parser


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    events, parse_errors = parse_jsonl(args.events)
    if parse_errors:
        for error in parse_errors:
            print(error, file=sys.stderr)
        return 2

    store = CDCStore(args.db, delete_mode=args.delete_mode)
    try:
        applier = CDCApplier(store)
        report = applier.apply(events)
        report.write_json(args.out)
        summary = report.summary
        print(
            f"events={len(events)} applied={summary.applied} "
            f"skipped={summary.skipped} final_rows={store.row_count()}"
        )
    finally:
        store.close()
    return 0


def main(argv: Optional[List[str]] = None) -> None:
    raise SystemExit(run(argv))


if __name__ == "__main__":
    main()
