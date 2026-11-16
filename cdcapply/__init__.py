"""Standard-library-only change-data-capture event applier."""

from .apply import CDCApplier
from .store import CDCStore, PHYSICAL_DELETE, SOFT_DELETE

__all__ = ["CDCApplier", "CDCStore", "PHYSICAL_DELETE", "SOFT_DELETE"]
