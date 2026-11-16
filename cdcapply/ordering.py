"""Per-primary-key timestamp ordering state."""

from __future__ import annotations

from typing import Any, Dict


class OrderingTracker:
    def __init__(self) -> None:
        self._max_ts: Dict[Any, Any] = {}

    @classmethod
    def from_map(cls, values: Dict[Any, Any]) -> "OrderingTracker":
        tracker = cls()
        tracker._max_ts.update(values)
        return tracker

    def is_out_of_order(self, primary_key: Any, ts: Any) -> bool:
        return primary_key in self._max_ts and ts < self._max_ts[primary_key]

    def advance(self, primary_key: Any, ts: Any) -> None:
        current = self._max_ts.get(primary_key)
        if current is None or ts > current:
            self._max_ts[primary_key] = ts

    def snapshot(self) -> Dict[Any, Any]:
        return dict(self._max_ts)
