"""Serialize operations that share a connection or mutable runtime state."""

from functools import wraps


def synchronized(fn):
    @wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapper
