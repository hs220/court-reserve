"""Shared classification of transient network failures.

Both the CLI (`main.py`) and the web job runner (`web/job_runner.py`) need to decide
whether an exception means "the site/network hiccuped, retry" or "this is a real
error, give up". Keeping one list here avoids the two copies drifting apart — run
#282 died because `ETIMEDOUT` had been added to neither.
"""

import json

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
