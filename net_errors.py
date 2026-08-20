"""Shared classification of transient network failures.

Both the CLI (`main.py`) and the web job runner (`web/job_runner.py`) need to decide
whether an exception means "the site/network hiccuped, retry" or "this is a real
error, give up". Keeping one list here avoids the two copies drifting apart — run
#282 died because `ETIMEDOUT` had been added to neither.
"""

import json
import time

# Treat any Playwright timeout (Page.goto, wait_for_selector, APIRequestContext.post,
# etc.) as a transient/retriable condition — the site is just slow or briefly
# unreachable. We'd rather retry than fail a booking/watch job outright.
NETWORK_ERROR_MARKERS = [
    # DNS / name resolution
    "eai_again", "getaddrinfo", "enotfound", "err_name_not_resolved",
    # generic Playwright/Chromium network failures
    "net::", "networkerror", "eof", "err_connection", "err_network",
    "err_internet_disconnected",
    # timeouts. libuv reports the raw errno code `ETIMEDOUT` with no separator, so
    # it matches none of the "timeout"/"timed out" spellings — it needs its own entry.
    "timeout", "timed out", "err_timed_out", "etimedout",
    # abrupt connection drops mid-request (seen from ReadConsolidated / Cloudflare)
    "socket hang up", "connection reset", "econnreset", "reset by peer",
    "err_connection_reset", "econnaborted",
    # connection could not be established at all
    "connection refused", "econnrefused", "ehostunreach", "enetunreach",
    "enetdown", "err_connection_refused",
    # upstream/proxy hiccups that return an error page instead of data
    "502", "503", "504", "bad gateway", "service unavailable", "gateway time",
]


def is_network_error(exc: Exception) -> bool:
    """True if `exc` looks like a transient network/availability blip worth retrying."""
    # A JSON decode failure from an API call means the site handed back an HTML error
    # or Cloudflare challenge page instead of the expected JSON — a transient blip, not
    # a real bug. Playwright's APIResponse.json() raises json.JSONDecodeError here.
    if isinstance(exc, json.JSONDecodeError):
        return True
    return any(k in str(exc).lower() for k in NETWORK_ERROR_MARKERS)


# How long a retry loop may keep failing before it gives up and reports a failure.
# Retrying forever is not resilience: a job whose every attempt fails still reads
# "running", so the one event that would tell you it isn't working — a failure — never
# happens. Run #331 restarted its browser session 1,151 times over 23 hours on the same
# error, told nobody, and never watched the court it was supposed to be watching.
GIVE_UP_AFTER_SECONDS = 30 * 60
# A long poll interval could otherwise cross that window on one or two attempts, which
# is too thin a basis for declaring a job dead.
GIVE_UP_MIN_ATTEMPTS = 3
# Early heads-up while the loop is still trying.
WARN_AFTER_ATTEMPTS = 20


class ErrorStreak:
    """An unbroken run of failures in a retry loop.

    Any loop that can retry indefinitely should keep one of these: `record(exc)` on
    every failed attempt, `clear()` on every attempt that succeeds. `record` returns
    True once the streak has lasted long enough that the loop should stop and let its
    caller report a failure.

    It is deliberately blind to what the error *is*: what makes a streak fatal is that
    it isn't clearing. A waiver gate, an expired login, a DNS outage and a wedged proxy
    all look identical from inside the loop, and all of them mean the same thing after
    half an hour — this job is not doing its job.
    """

    def __init__(self, give_up_after_seconds: float = GIVE_UP_AFTER_SECONDS,
                 min_attempts: int = GIVE_UP_MIN_ATTEMPTS,
                 warn_after: int = WARN_AFTER_ATTEMPTS,
                 clock=time.monotonic):
        self._give_up_after = give_up_after_seconds
        self._min_attempts = min_attempts
        self._warn_after = warn_after
        self._clock = clock
        self.count = 0
        self.first_at = None
        self.last_error = None
        self.warned = False

    def record(self, exc) -> bool:
        """Note one failed attempt. True means: stop retrying, this isn't clearing."""
        self.count += 1
        self.last_error = exc
        if self.first_at is None:
            self.first_at = self._clock()
        return self.elapsed >= self._give_up_after and self.count >= self._min_attempts

    def clear(self) -> None:
        """An attempt succeeded — the streak is over."""
        self.count = 0
        self.first_at = None
        self.last_error = None
        self.warned = False

    def should_warn(self) -> bool:
        """True exactly once per streak, the first time it crosses the warn threshold.
        Once per streak, not once every N errors: the streak now ends in a failure
        notification of its own, so repeating the warning is just noise."""
        if self.warned or self.count < self._warn_after:
            return False
        self.warned = True
        return True

    @property
    def elapsed(self) -> float:
        """Seconds since the streak's first failure (0 when there is no streak)."""
        return 0.0 if self.first_at is None else self._clock() - self.first_at

    def describe(self) -> str:
        return (f"{self.count} consecutive failures over {self.elapsed / 60:.0f}m; "
                f"last error: {self.last_error}")
