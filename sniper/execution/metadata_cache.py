"""Small bounded cache for immutable execution metadata only.

The loader runs outside the lock.  Concurrent misses may duplicate one read, but
unrelated token lookups never serialize behind a network request.
"""
from __future__ import annotations

from collections import OrderedDict
from threading import Lock


_MISSING = object()


class BoundedLru:
    """Thread-safe bounded LRU which never caches ``None`` or loader failures."""

    def __init__(self, maxsize):
        maxsize = int(maxsize)
        if maxsize <= 0:
            raise ValueError("maxsize must be positive")
        self.maxsize = maxsize
        self._values = OrderedDict()
        self._lock = Lock()

    def get_or_load(self, key, loader):
        with self._lock:
            value = self._values.get(key, _MISSING)
            if value is not _MISSING:
                self._values.move_to_end(key)
                return value

        value = loader()
        if value is None:
            return None

        with self._lock:
            existing = self._values.get(key, _MISSING)
            if existing is not _MISSING:
                self._values.move_to_end(key)
                return existing
            self._values[key] = value
            self._values.move_to_end(key)
            while len(self._values) > self.maxsize:
                self._values.popitem(last=False)
        return value

    def __len__(self):
        with self._lock:
            return len(self._values)
