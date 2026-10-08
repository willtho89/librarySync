"""In-process throttling of failed logins per client address and per username.

The API runs as a single process in the supported deployments, so a
process-local sliding window is enough to slow down password guessing
without a new table.
"""

import time
from collections import defaultdict, deque

WINDOW_SECONDS = 15 * 60
MAX_FAILURES_PER_USERNAME = 10
MAX_FAILURES_PER_ADDRESS = 30


class LoginThrottle:
    def __init__(self) -> None:
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _recent(self, key: str, now: float) -> deque[float]:
        attempts = self._failures[key]
        while attempts and attempts[0] <= now - WINDOW_SECONDS:
            attempts.popleft()
        if not attempts:
            self._failures.pop(key, None)
            return deque()
        return attempts

    def retry_after(self, username: str, address: str | None, now: float | None = None) -> int | None:
        """Seconds until another attempt is allowed, or None when not throttled."""
        now = time.monotonic() if now is None else now
        waits: list[float] = []
        for key, limit in self._keys(username, address):
            attempts = self._recent(key, now)
            if len(attempts) >= limit:
                waits.append(attempts[0] + WINDOW_SECONDS - now)
        return max(1, int(max(waits))) if waits else None

    def record_failure(self, username: str, address: str | None, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        for key, _limit in self._keys(username, address):
            self._failures[key].append(now)

    def reset(self, username: str) -> None:
        self._failures.pop(f"user:{username}", None)

    @staticmethod
    def _keys(username: str, address: str | None) -> list[tuple[str, int]]:
        keys = [(f"user:{username}", MAX_FAILURES_PER_USERNAME)]
        if address:
            keys.append((f"addr:{address}", MAX_FAILURES_PER_ADDRESS))
        return keys


LOGIN_THROTTLE = LoginThrottle()
