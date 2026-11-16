"""Transaction grouping helpers for CDC event streams."""

from __future__ import annotations

from typing import List, Tuple

from .events import Event


def group_by_transaction(events: List[Event]) -> List[Tuple[object, List[Event]]]:
    grouped: List[Tuple[object, List[Event]]] = []
    for event in events:
        if not grouped or grouped[-1][0] != event.txn:
            grouped.append((event.txn, [event]))
        else:
            grouped[-1][1].append(event)
    return grouped


def sort_transaction_events(events: List[Event]) -> List[Event]:
    return sorted(events, key=lambda event: (event.ts, event.event_id))


def rollback_reason(original_error: str, event_count: int) -> str:
    if event_count > 1:
        return f"事务被回滚：{original_error}"
    return original_error
