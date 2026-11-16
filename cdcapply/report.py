"""Processing report data structures and JSON output."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from typing import List


@dataclass(frozen=True)
class ReportEntry:
    event_id: str
    op: str
    applied: bool
    reason: str
    table_rows: int


@dataclass(frozen=True)
class ReportSummary:
    applied: int = 0
    skipped: int = 0
    duplicates: int = 0
    out_of_order_dropped: int = 0
    rolled_back_txns: int = 0


class Report:
    def __init__(self) -> None:
        self.entries: List[ReportEntry] = []
        self.summary = ReportSummary()

    def add_entry(
        self,
        event_id: str,
        op: str,
        applied: bool,
        reason: str,
        table_rows: int,
        *,
        duplicate: bool = False,
        out_of_order: bool = False,
    ) -> None:
        self.entries.append(
            ReportEntry(
                event_id=event_id,
                op=op,
                applied=applied,
                reason=reason,
                table_rows=table_rows,
            )
        )
        if applied:
            self.summary = ReportSummary(**{**asdict(self.summary), "applied": self.summary.applied + 1})
        else:
            self.summary = ReportSummary(**{**asdict(self.summary), "skipped": self.summary.skipped + 1})
        if duplicate:
            self.summary = ReportSummary(
                **{**asdict(self.summary), "duplicates": self.summary.duplicates + 1}
            )
        if out_of_order:
            self.summary = ReportSummary(
                **{
                    **asdict(self.summary),
                    "out_of_order_dropped": self.summary.out_of_order_dropped + 1,
                }
            )

    def increment_rolled_back_txns(self) -> None:
        self.summary = ReportSummary(
            **{**asdict(self.summary), "rolled_back_txns": self.summary.rolled_back_txns + 1}
        )

    def to_dict(self) -> dict:
        return {
            "events": [asdict(entry) for entry in self.entries],
            "summary": asdict(self.summary),
        }

    def write_json(self, path: str) -> None:
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as target:
            json.dump(self.to_dict(), target, ensure_ascii=False, indent=2)
            target.write("\n")
