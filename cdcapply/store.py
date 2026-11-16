"""SQLite-backed table storage for applied CDC events."""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .events import DELETE, Event


SOFT_DELETE = "soft"
PHYSICAL_DELETE = "physical"
VALID_DELETE_MODES = frozenset((SOFT_DELETE, PHYSICAL_DELETE))


class CDCStore:
    def __init__(self, path: str, delete_mode: str = SOFT_DELETE) -> None:
        if delete_mode not in VALID_DELETE_MODES:
            raise ValueError(f"unsupported delete mode: {delete_mode}")
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self.path = path
        self.delete_mode = delete_mode
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self._create_schema()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS cdc_rows (
                primary_key TEXT PRIMARY KEY,
                row_json TEXT NOT NULL,
                max_ts REAL NOT NULL,
                last_event_id TEXT NOT NULL,
                deleted INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS cdc_event_ids (
                event_id TEXT PRIMARY KEY
            );
            CREATE INDEX IF NOT EXISTS idx_cdc_rows_deleted ON cdc_rows(deleted);
            """
        )
        self.connection.commit()

    def has_event(self, event_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM cdc_event_ids WHERE event_id = ?", (event_id,)
        ).fetchone()
        return row is not None

    def existing_events(self, event_ids: Iterable[str]) -> set:
        ids = list(event_ids)
        if not ids:
            return set()
        placeholders = ", ".join("?" for _ in ids)
        rows = self.connection.execute(
            f"SELECT event_id FROM cdc_event_ids WHERE event_id IN ({placeholders})", ids
        ).fetchall()
        return {row["event_id"] for row in rows}

    def get_row(self, primary_key: Any) -> Optional[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM cdc_rows WHERE primary_key = ?", (self._key(primary_key),)
        ).fetchone()

    def all_rows(self) -> List[sqlite3.Row]:
        return list(
            self.connection.execute(
                "SELECT * FROM cdc_rows ORDER BY primary_key ASC"
            ).fetchall()
        )

    def row_count(self) -> int:
        return self.connection.execute("SELECT COUNT(*) FROM cdc_rows").fetchone()[0]

    def max_ts_by_key(self) -> Dict[str, Any]:
        rows = self.connection.execute("SELECT primary_key, max_ts FROM cdc_rows").fetchall()
        return {json.loads(row["primary_key"]): row["max_ts"] for row in rows}

    def snapshot_json_lines(self) -> bytes:
        lines = []
        for row in self.all_rows():
            lines.append(json.dumps(json.loads(row["row_json"]), sort_keys=True, ensure_ascii=False))
        return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")

    def apply_events(self, events: Sequence[Event]) -> None:
        if not events:
            return
        try:
            with self.connection:
                for event in events:
                    self._apply_one(event)
                self.connection.executemany(
                    "INSERT INTO cdc_event_ids(event_id) VALUES (?)",
                    [(event.event_id,) for event in events],
                )
        except Exception:
            self.connection.rollback()
            raise

    def _apply_one(self, event: Event) -> None:
        if event.op == DELETE:
            self._apply_delete(event)
            return
        row = dict(event.after)
        row["primary_key"] = event.primary_key
        row["deleted"] = bool(row.get("deleted", False))
        self._upsert_row(event, row, deleted=bool(row.get("deleted", False)))

    def _apply_delete(self, event: Event) -> None:
        if self.delete_mode == PHYSICAL_DELETE:
            self.connection.execute(
                "DELETE FROM cdc_rows WHERE primary_key = ?", (self._key(event.primary_key),)
            )
            return
        existing = self.get_row(event.primary_key)
        if existing is None:
            row = {"primary_key": event.primary_key, "deleted": True}
        else:
            row = json.loads(existing["row_json"])
            row["deleted"] = True
        self._upsert_row(event, row, deleted=True)

    def _upsert_row(self, event: Event, row: Dict[str, Any], deleted: bool) -> None:
        encoded = json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        self.connection.execute(
            """
            INSERT INTO cdc_rows(primary_key, row_json, max_ts, last_event_id, deleted)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(primary_key) DO UPDATE SET
                row_json = excluded.row_json,
                max_ts = excluded.max_ts,
                last_event_id = excluded.last_event_id,
                deleted = excluded.deleted
            """,
            (self._key(event.primary_key), encoded, event.ts, event.event_id, int(deleted)),
        )

    def _key(self, primary_key: Any) -> str:
        return json.dumps(primary_key, sort_keys=True, ensure_ascii=False)

    def close(self) -> None:
        self.connection.close()
