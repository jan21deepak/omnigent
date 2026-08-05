"""Shared in-process rate limiting for the unauthenticated OAuth surface."""

from __future__ import annotations

# Hard cap on distinct keys a limiter tracks at once. Bounds memory even
# under a spray from many source IPs (e.g. a whole IPv6 /64) — without it a
# key hit once and never revisited would live forever. When the cap is hit
# the whole table is swept of aged-out keys; if still full, the limiter
# fails OPEN for a new key (availability over a soft throttle — the real
# anti-abuse control in production is the confidential client secret).
RATE_LIMITER_MAX_KEYS = 10_000


class SlidingWindowRateLimiter:
    """Minimal per-key sliding-window limiter (in-memory, single-process).

    Keyed by client IP. Adequate for a single-process deployment; a
    multi-replica server would want a shared store.

    Memory is bounded by :data:`RATE_LIMITER_MAX_KEYS`: keys are dropped
    when they age out (on touch) and, when the cap is reached, a full sweep
    reclaims every aged-out key before admitting a new one.
    """

    def __init__(self, max_events: int, window_seconds: int, max_keys: int) -> None:
        self._max = max_events
        self._window = window_seconds
        self._max_keys = max_keys
        self._hits: dict[str, list[float]] = {}

    def _sweep(self, cutoff: float) -> None:
        """Drop every key whose hits have all aged out."""
        dead = [k for k, ts in self._hits.items() if not any(t > cutoff for t in ts)]
        for k in dead:
            self._hits.pop(k, None)

    def _live(self, key: str, cutoff: float) -> list[float]:
        hits = [t for t in self._hits.get(key, ()) if t > cutoff]
        # Opportunistically bound memory: drop keys that fully aged out.
        if hits:
            self._hits[key] = hits
        else:
            self._hits.pop(key, None)
        return hits

    def _at_capacity(self, key: str, cutoff: float) -> bool:
        """True when a new key cannot be admitted without unbounded growth."""
        if key in self._hits or len(self._hits) < self._max_keys:
            return False
        self._sweep(cutoff)
        return len(self._hits) >= self._max_keys

    def exhausted(self, key: str, now: float) -> bool:
        """Return True if ``key`` has spent its budget, without charging it."""
        cutoff = now - self._window
        if self._at_capacity(key, cutoff):
            return False
        return len(self._live(key, cutoff)) >= self._max

    def record(self, key: str, now: float) -> None:
        """Charge one event against ``key``."""
        cutoff = now - self._window
        if self._at_capacity(key, cutoff):
            return
        self._hits.setdefault(key, self._live(key, cutoff)).append(now)

    def allow(self, key: str, now: float) -> bool:
        """Charge one event against ``key``, or refuse when out of budget."""
        if self.exhausted(key, now):
            return False
        self.record(key, now)
        return True
