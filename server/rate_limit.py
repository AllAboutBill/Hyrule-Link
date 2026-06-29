"""Small in-process sliding-window limiter for public room endpoints."""

import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, limit: int, window_s: float = 60.0):
        self.limit = int(limit)
        self.window_s = float(window_s)
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, now=None) -> bool:
        now = time.monotonic() if now is None else float(now)
        cutoff = now - self.window_s
        with self._lock:
            hits = self._hits[key]
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True


def client_key(request) -> str:
    """Use X-Forwarded-For only from a loopback reverse proxy."""
    peer = request.client.host if request.client else "unknown"
    if peer in ("127.0.0.1", "::1"):
        # Use the hop appended by our immediate proxy. Taking the first value
        # would let a client prepend a fake address and bypass the limiter.
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[-1].strip()
        if forwarded:
            return forwarded
    return peer
