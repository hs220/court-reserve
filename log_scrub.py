"""Take session cookies and auth headers out of text before it is stored or sent.

When a request fails, Playwright appends a call log to the exception, and that log lists
the request's headers -- including `cookie: ASP.NET_SessionId=...;
__RequestVerificationToken=...`. Runs print their exceptions, and run output is stored
in `job_runs.log_text`, shown on the web UI (which has no auth), tailed into failure
emails and handed to the home agent. A cookie there is a live CourtReserve login for
whoever reads it (runs 403 and 411 held one each).

So every path that stores or sends run text passes it through `scrub` first. It is
deliberately blunt: a whole header line goes, and so does any value that merely looks
like one of CourtReserve's cookies. Over-removing a look-alike costs a little debugging;
under-removing hands out a session. `scrub` is idempotent -- it leaves its own markers
alone -- so text can go through it more than once (scripts/scrub_run_logs.py relies on
that for rows written before this existed).

Stdlib only: scripts/scrub_run_logs.py imports it without the app's dependencies.
"""

import re
from collections import Counter

COOKIE_MARK = "[cookie removed]"
AUTH_MARK = "[authorization removed]"

_HEADERS = r"set-cookie|cookie|proxy-authorization|authorization"
# Already scrubbed: leave it, so a second pass changes (and counts) nothing.
_NOT_DONE = r"(?!\s*\[(?:cookie|authorization) removed\])"

# CourtReserve is ASP.NET: the session, the anti-forgery token (ASP.NET MVC adds a
# path suffix to its name, e.g. `__RequestVerificationToken_Lw__`) and the OWIN /
# ASP.NET Core auth cookies.
_COOKIE_NAMES = (r"ASP\.NET_SessionId|__RequestVerificationToken\w*"
                 r"|\.AspNet\.[\w.\-]+|\.AspNetCore\.[\w.\-]+")


def _mark(header: str) -> str:
    return AUTH_MARK if "authorization" in header.lower() else COOKIE_MARK


# (name, pattern, replacement), applied in order.
_RULES = [
    # `"cookie": "..."` -- a headers dict printed as JSON or a Python repr.
    ("quoted_header",
     re.compile(rf"""(?i)(["'])({_HEADERS})\1(\s*:\s*)(["']){_NOT_DONE}(?:\\.|(?!\4).)*\4"""),
     lambda m: f"{m[1]}{m[2]}{m[1]}{m[3]}{m[4]}{_mark(m[2])}{m[4]}"),
    # `  -   cookie: ...` -- Playwright's call log, and any other `Header: value` line.
    # The rest of the line goes, whatever its indentation or case.
    ("header_line",
     re.compile(rf"(?i)(?<![\w-])({_HEADERS})([ \t]*:[ \t]*){_NOT_DONE}[^\r\n]*"),
     lambda m: f"{m[1]}{m[2]}{_mark(m[1])}"),
    # `ASP.NET_SessionId=...` anywhere: a form body, a URL, a cookie string on its own.
    ("named_cookie",
     re.compile(rf"(?i)(?<![\w.])({_COOKIE_NAMES})(\s*=\s*){_NOT_DONE}[^;,\s\"'&<>]+"),
     lambda m: f"{m[1]}{m[2]}{COOKIE_MARK}"),
    # `"ASP.NET_SessionId": "..."` -- the same, as a JSON or dict key.
    ("named_cookie",
     re.compile(rf"""(?i)(["'])({_COOKIE_NAMES})\1(\s*:\s*)(["']){_NOT_DONE}[^"']*\4"""),
     lambda m: f"{m[1]}{m[2]}{m[1]}{m[3]}{m[4]}{COOKIE_MARK}{m[4]}"),
    # `{"name": "...", "value": "..."}` -- Playwright's storage state / context.cookies().
    ("cookie_record",
     re.compile(r"""(?i)(["']name["']\s*:\s*["'][^"']*["']\s*,\s*["']value["']\s*:\s*)"""
                rf"""(["']){_NOT_DONE}[^"']*\2"""),
     lambda m: f"{m[1]}{m[2]}{COOKIE_MARK}{m[2]}"),
    # `Bearer <token>` wherever it appears.
    ("bearer",
     re.compile(rf"(?i)\b(bearer\s+){_NOT_DONE}[A-Za-z0-9._~+/\-]{{16,}}=*"),
     lambda m: f"{m[1]}{AUTH_MARK}"),
]

# Any other cookie: on a line that mentions cookies, a `name=value` whose value is long
# enough to be a token. Short ones (`path=/`, `samesite=lax`) stay readable.
_COOKIE_LINE = re.compile(r"(?i)^[^\r\n]*cookie[^\r\n]*$", re.MULTILINE)
_LONG_VALUE = re.compile(rf"([\w.\-]+)(\s*=\s*){_NOT_DONE}[^;,\s\"'&<>]{{16,}}")


def scrub_counted(text: str) -> tuple[str, Counter]:
    """`scrub`, also returning how many values each rule removed."""
    counts: Counter = Counter()
    if not text:
        return text, counts
    for name, pattern, repl in _RULES:
        text, n = pattern.subn(repl, text)
        counts[name] += n

    def long_values(line):
        out, n = _LONG_VALUE.subn(lambda m: f"{m[1]}{m[2]}{COOKIE_MARK}", line[0])
        counts["long_cookie_value"] += n
        return out

    text = _COOKIE_LINE.sub(long_values, text)
    return text, +counts


def scrub(text: str) -> str:
    """`text` with cookie and authorization values replaced by fixed markers."""
    return scrub_counted(text)[0]
