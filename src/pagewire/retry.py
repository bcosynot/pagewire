"""Retrying a service that said "later" rather than "no".

The statuses below are the ones that mean *come back in a moment*: a rate limit, or the
service having a bad moment. Everything else — a bad token, a missing scope, a document it
could not parse — is an answer, and retrying an answer only repeats it while making the
queue slower to clear.

Kept out of either client because both refuse in the same shapes, and out of the delivery
module because its retry means something else: there, a second attempt is about finding out
whether an earlier one wrote something.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

# 429 is a rate limit; the 5xx family is the service failing to answer this time. Both are
# worth waiting out; a 403 or a 422 is not.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class RetryPolicy:
    """How many times to try, and how long to wait between attempts.

    `attempts` counts the first try, so `attempts=3` means two waits. The wait doubles
    from `backoff_seconds` and stops growing at `backoff_max_seconds`, which is what keeps
    a service that has been down for a while from turning one book into a long sleep.
    """

    attempts: int = 4
    backoff_seconds: float = 2.0
    backoff_max_seconds: float = 60.0
    sleep: Callable[[float], None] = time.sleep

    def wait_for(self, attempt: int) -> float:
        """How long to wait before the attempt after `attempt` (0-based)."""
        return min(self.backoff_seconds * (2**attempt), self.backoff_max_seconds)

    def pause(self, attempt: int) -> float:
        """Wait before the next attempt. Returns how long it waited, for the log."""
        seconds = self.wait_for(attempt)
        self.sleep(seconds)
        return seconds
