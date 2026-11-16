"""Application engine combining ordering, idempotency, and transactions."""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from .events import Event, validate_event
from .ordering import OrderingTracker
from .report import Report
from .store import CDCStore
from .transactions import group_by_transaction, rollback_reason, sort_transaction_events


class CDCApplier:
    def __init__(self, store: CDCStore) -> None:
        self.store = store
        self.ordering = OrderingTracker.from_map(store.max_ts_by_key())
        self.report = Report()
        self.processed_event_ids = set()

    def apply(self, events: Sequence[Event]) -> Report:
        for txn, txn_events in group_by_transaction(list(events)):
            self._apply_transaction(txn, txn_events)
        return self.report

    def _apply_transaction(self, txn: object, events: List[Event]) -> None:
        invalid_result = self._first_invalid_event(events)
        if invalid_result is not None:
            event, reason = invalid_result
            self._record_rollback(
                events,
                reason,
                rollback_reason(reason, len(events)),
            )
            return

        duplicate_ids = self._duplicate_ids(events)
        candidate_events = [event for event in events if event.event_id not in duplicate_ids]

        applied_events: List[Event] = []
        for event in events:
            if event.event_id in duplicate_ids:
                self.report.add_entry(
                    event.event_id,
                    event.op,
                    False,
                    "duplicate event_id; event not applied",
                    self.store.row_count(),
                    duplicate=True,
                )
                continue

            if self.ordering.is_out_of_order(event.primary_key, event.ts):
                reason = (
                    "out of order: ts "
                    + str(event.ts)
                    + " is earlier than applied max ts for primary key "
                    + str(event.primary_key)
                )
                self.report.add_entry(
                    event.event_id,
                    event.op,
                    False,
                    reason,
                    self.store.row_count(),
                    out_of_order=True,
                )
                continue

            applied_events.append(event)

        if applied_events:
            ordered_events = sort_transaction_events(applied_events)
            self.store.apply_events(ordered_events)
            for event in ordered_events:
                self.ordering.advance(event.primary_key, event.ts)
                self.processed_event_ids.add(event.event_id)
            applied_ids = {event.event_id for event in ordered_events}
            final_rows = self.store.row_count()
            for event in events:
                if event.event_id in applied_ids:
                    self.report.add_entry(
                        event.event_id,
                        event.op,
                        True,
                        "applied",
                        final_rows,
                    )

    def _first_invalid_event(self, events: List[Event]) -> Optional[Tuple[Event, str]]:
        for event in events:
            reason = validate_event(event)
            if reason is not None:
                return event, reason
        return None

    def _record_rollback(
        self,
        events: List[Event],
        original_reason: str,
        entry_reason: str,
    ) -> None:
        row_count = self.store.row_count()
        for event in events:
            self.report.add_entry(
                event.event_id,
                event.op,
                False,
                entry_reason,
                row_count,
            )
        self.report.increment_rolled_back_txns()

    def _duplicate_ids(self, events: List[Event]) -> set:
        stored = self.store.existing_events(event.event_id for event in events)
        seen = set()
        repeated = set()
        for event in events:
            if event.event_id in seen:
                repeated.add(event.event_id)
            seen.add(event.event_id)
        return stored | self.processed_event_ids | repeated
