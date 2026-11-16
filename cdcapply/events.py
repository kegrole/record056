"""JSON Lines change-data-capture event parsing and validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple


INSERT = "insert"
UPDATE = "update"
DELETE = "delete"
VALID_OPS = frozenset((INSERT, UPDATE, DELETE))
REQUIRED_AFTER_FIELDS = ("name", "value")
FIELD_TYPES = {
    "name": str,
    "value": (int, float),
    "deleted": bool,
}


@dataclass(frozen=True)
class Event:
    event_id: str
    op: str
    primary_key: Any
    txn: Any
    ts: Any
    before: Optional[Dict[str, Any]]
    after: Optional[Dict[str, Any]]
    raw: Dict[str, Any]
    line_number: int


def _build_event(raw: Dict[str, Any], line_number: int) -> Event:
    return Event(
        event_id=raw.get("event_id"),
        op=raw.get("op"),
        primary_key=raw.get("primary_key"),
        txn=raw.get("txn"),
        ts=raw.get("ts"),
        before=raw.get("before"),
        after=raw.get("after"),
        raw=raw,
        line_number=line_number,
    )


def parse_jsonl(path: str) -> Tuple[List[Event], List[str]]:
    """Parse a JSON Lines file, returning parsed events and parser errors."""
    events: List[Event] = []
    errors: List[str] = []
    with open(path, "r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"line {line_number}: invalid JSON: {exc.msg}")
                continue
            if not isinstance(raw, dict):
                errors.append(f"line {line_number}: event must be a JSON object")
                continue
            events.append(_build_event(raw, line_number))
    return events, errors


def parse_events(values: Iterable[Dict[str, Any]]) -> List[Event]:
    """Build events from in-memory dictionaries, primarily useful for tests."""
    return [_build_event(dict(value), index) for index, value in enumerate(values, start=1)]


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_supported_key(value: Any) -> bool:
    return isinstance(value, (str, int, float)) and not isinstance(value, bool)


def _validate_after_fields(after: Dict[str, Any]) -> Optional[str]:
    missing = [field for field in REQUIRED_AFTER_FIELDS if field not in after]
    if missing:
        return "missing required after field(s): " + ", ".join(missing)

    type_errors = []
    for field, expected_type in FIELD_TYPES.items():
        if field not in after:
            continue
        value = after[field]
        if isinstance(expected_type, tuple):
            valid = _is_number(value) if field == "value" else isinstance(value, expected_type)
        else:
            valid = isinstance(value, expected_type)
        if not valid:
            type_errors.append(field)
    if type_errors:
        return "invalid type for after field(s): " + ", ".join(type_errors)
    return None


def validate_event(event: Event) -> Optional[str]:
    """Return a validation reason, or None when the event is valid."""
    if not isinstance(event.event_id, str) or not event.event_id:
        return "missing or invalid event_id"
    if not _is_supported_key(event.txn):
        return "missing or invalid txn"
    if not _is_number(event.ts):
        return "missing or invalid ts"
    if event.op not in VALID_OPS:
        return "missing or invalid op"
    if not _is_supported_key(event.primary_key):
        return "missing or invalid primary_key"
    if event.before is not None and not isinstance(event.before, dict):
        return "before must be an object"
    if event.op in (INSERT, UPDATE):
        if not isinstance(event.after, dict):
            return "missing or invalid after"
        after_error = _validate_after_fields(event.after)
        if after_error is not None:
            return after_error
    elif event.after is not None and not isinstance(event.after, dict):
        return "after must be an object"
    return None
