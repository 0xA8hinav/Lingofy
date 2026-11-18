"""Retry helper + dumb breaker ($0 demo path)."""

import random
import time


def call_with_retry(fn, retries=2):
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as e:
            code = getattr(e, "status", 0)
            retryable = code in (429, 500, 502, 503, 504) or "429" in str(e) or "5" in str(e)[:1]
            if attempt >= retries or not retryable:
                raise
            time.sleep(random.uniform(0, min(2**attempt, 8)))
            attempt += 1


class DumbBreaker:
    def __init__(self, failures=3, fallback_s=60):
        self.threshold = failures
        self.fallback_s = fallback_s
        self._fails: dict[str, int] = {}
        self._open_until: dict[str, float] = {}

    def record_failure(self, provider: str):
        self._fails[provider] = self._fails.get(provider, 0) + 1
        if self._fails[provider] >= self.threshold:
            self._open_until[provider] = time.time() + self.fallback_s

    def use_fallback(self, provider: str) -> bool:
        return time.time() < self._open_until.get(provider, 0)
