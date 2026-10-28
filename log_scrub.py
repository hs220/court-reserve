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

Every pattern is linear in its input: this runs on third-party text (CourtReserve's
responses, Playwright's errors), inside print() and inside web requests, so a pattern
that backtracks would let one odd line stall a job or the whole app. Repetitions are
built so that each character can be matched only one way, and a line longer than
MAX_LINE is not scanned at all: if it mentions anything cookie-like it is removed
whole. test_log_scrub times each pattern on adversarial input.

Stdlib only: scripts/scrub_run_logs.py imports it without the app's dependencies.
"""

import re
from collections import Counter

COOKIE_MARK = "[cookie removed]"
AUTH_MARK = "[authorization removed]"
TOKEN_MARK = "[token removed]"

# Longer lines aren't scanned; if they mention any of TRIGGERS they go whole.
MAX_LINE = 16 * 1024
TRIGGERS = ("cookie", "authoriz", "bearer", "token", "sessionid", "aspnet", "value")

# The anti-forgery token also travels as a header of its own.
_TOKEN_HEADERS = (r"x-requestverificationtoken|requestverificationtoken"
                  r"|x-xsrf-token|xsrf-token|x-csrf-token|csrf-token")
_HEADERS = rf"set-cookie|cookie|proxy-authorization|authorization|{_TOKEN_HEADERS}"
# Already scrubbed: leave it, so a second pass changes (and counts) nothing.
_NOT_DONE = r"(?!\s*\[(?:cookie|authorization|token) removed\])"
# `:` and `=` as typed, percent-encoded (a URL, a form body) or escaped (JSON, a repr).
_COLON = r"(?::|%3[Aa]|\\u003[Aa]|\\x3[Aa])"
_EQUALS = r"(?:=|%3[Dd]|\\u003[Dd]|\\x3[Dd])"
# A cookie value: up to `;`, `,`, whitespace, a quote, `&` or `<>`. An encoded
# separator (`%3B`) doesn't stop it, so an encoded cookie string goes whole.
_VALUE = r"""[^;,\s"'&<>]+"""
# A quoted string's body. A backslash always starts an escape, never a plain
# character, so each character matches one way and an unclosed quote fails fast.
_QUOTED = r"(?:\\[^\r\n]|(?!{q})[^\\\r\n])*{q}"

# CourtReserve is ASP.NET: the session, the anti-forgery token (ASP.NET MVC adds a
# path suffix to its name, e.g. `__RequestVerificationToken_Lw__`) and the OWIN /
# ASP.NET Core auth cookies.
_COOKIE_NAMES = (r"ASP\.NET_SessionId|__RequestVerificationToken\w*|\.ASPXAUTH"
                 r"|\.AspNet\.[\w.\-]+|\.AspNetCore\.[\w.\-]+")


def _mark(header: str) -> str:
    header = header.lower()
    if "authorization" in header:
        return AUTH_MARK
    return COOKIE_MARK if "cookie" in header else TOKEN_MARK


# (name, pattern, replacement), applied in order.
_RULES = [
    # `"cookie": "..."` -- a headers dict printed as JSON or a Python repr.
    ("quoted_header",
     re.compile(rf"""(?i)(["'])({_HEADERS})\1(\s*:\s*)(["']){_NOT_DONE}"""
                + _QUOTED.format(q=r"\4")),
     lambda m: f"{m[1]}{m[2]}{m[1]}{m[3]}{m[4]}{_mark(m[2])}{m[4]}"),
    # `  -   cookie: ...` -- Playwright's call log, and any other `Header: value` line.
    # The rest of the line goes, whatever its indentation or case, and so do any lines
    # folded onto it (indented, but not the call log's next `  -   ` entry).
    ("header_line",
     re.compile(rf"(?i)(?<![A-Za-z])({_HEADERS})([ \t]*{_COLON}[ \t]*){_NOT_DONE}[^\r\n]*"
                r"((?:\r?\n(?![ \t]*-[ \t])[ \t]+\S[^\r\n]*)*)"),
     lambda m: f"{m[1]}{m[2]}{_mark(m[1])}"
               + re.sub(r"(\n[ \t]+)[^\r\n]*", rf"\1{_mark(m[1])}", m[3])),
    # `ASP.NET_SessionId=...` anywhere: a form body, a URL, a cookie string on its own.
    ("named_cookie",
     re.compile(rf"(?i)({_COOKIE_NAMES})(\s*{_EQUALS}\s*){_NOT_DONE}{_VALUE}"),
     lambda m: f"{m[1]}{m[2]}{COOKIE_MARK}"),
    # `"ASP.NET_SessionId": "..."` -- the same, as a JSON or dict key.
    ("named_cookie",
     re.compile(rf"""(?i)(["'])({_COOKIE_NAMES})\1(\s*:\s*)(["']){_NOT_DONE}[^"'\r\n]*\4"""),
     lambda m: f"{m[1]}{m[2]}{m[1]}{m[3]}{m[4]}{COOKIE_MARK}{m[4]}"),
    # `Bearer <token>` wherever it appears.
    ("bearer",
     re.compile(rf"(?i)\b(bearer\s+){_NOT_DONE}[A-Za-z0-9._~+/\-]{{16,}}=*"),
     lambda m: f"{m[1]}{AUTH_MARK}"),
]

# `{"name": "...", "value": "...", ...}` in either order -- Playwright's storage state
# and context.cookies(), on one line or pretty-printed: the value of any flat object
# that also has a "name".
_OBJECT = re.compile(r"\{[^{}]*\}")
_HAS_NAME = re.compile(r"""["']name["']\s*:""")
_RECORD_VALUE = re.compile(rf"""(["']value["']\s*:\s*)(["']){_NOT_DONE}""" + _QUOTED.format(q=r"\2"))

# Any other cookie: on a line that mentions cookies, a `name=value` whose value is long
# enough to be a token. Short ones (`path=/`, `samesite=lax`) stay readable. The
# lookbehind starts a name only where a word starts, which keeps this linear.
_COOKIE_LINE = re.compile(r"(?i)^[^\r\n]*cookie[^\r\n]*$", re.MULTILINE)
_LONG_VALUE = re.compile(rf"(?<![\w.\-])([\w.\-]+)(\s*=\s*){_NOT_DONE}[^;,\s\"'&<>]{{16,}}")


def _long_line(line: str) -> str:
    return f"[line of {len(line)} characters removed: too long to scrub, may hold a cookie]"


def scrub_counted(text: str) -> tuple[str, Counter]:
    """`scrub`, also returning how many values each rule removed."""
    counts: Counter = Counter()
    if not text:
        return text, counts
    if len(text) > MAX_LINE:
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if len(line) > MAX_LINE and any(t in line.lower() for t in TRIGGERS):
                lines[i] = _long_line(line)
                counts["long_line"] += 1
        text = "\n".join(lines)
    for name, pattern, repl in _RULES:
        text, n = pattern.subn(repl, text)
        counts[name] += n

    def record(obj):
        if not _HAS_NAME.search(obj[0]):
            return obj[0]
        out, n = _RECORD_VALUE.subn(lambda m: f"{m[1]}{m[2]}{COOKIE_MARK}{m[2]}", obj[0])
        counts["cookie_record"] += n
        return out

    text = _OBJECT.sub(record, text)

    def long_values(line):
        out, n = _LONG_VALUE.subn(lambda m: f"{m[1]}{m[2]}{COOKIE_MARK}", line[0])
        counts["long_cookie_value"] += n
        return out

    text = _COOKIE_LINE.sub(long_values, text)
    return text, +counts


def scrub(text: str) -> str:
    """`text` with cookie, token and authorization values replaced by fixed markers."""
    return scrub_counted(text)[0]
