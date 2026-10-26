"""Tests for log_scrub.py and scripts/scrub_run_logs.py.

A Playwright call log lists the failed request's headers, cookie included, and runs
403 and 411 stored one. Every value below is synthetic; each carries a SECRET marker
so a single assertNotIn proves it's gone.

Run:  python -m unittest test_log_scrub -v
"""

import io
import os
import sqlite3
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from log_scrub import AUTH_MARK, COOKIE_MARK, scrub, scrub_counted
from scripts import scrub_run_logs

SESSION = "SECRETsess" + "q1w2e3r4t5y6u7i8"
XSRF = "SECRETxsrf-" + "AbCdEfGhIjKlMnOp_0123456789"
APP_COOKIE = "SECRETapp" + "Zz9" * 40
CORE = "SECRETcore" + "CfDJ8Abc" * 6
BEARER = "SECRETbearer" + "eyJhbGciOiJIUzI1NiJ9"

# Shaped like Playwright's call log for a failed APIRequestContext.post (ReadConsolidated).
CALL_LOG = f"""[2026-10-24 10:15:02] Poll error (session restart): APIRequestContext.post: read ECONNRESET
Call log:
  - → POST https://app.courtreserve.com/Online/Reservations/ReadConsolidated/13234
  -   user-agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 HeadlessChrome/129.0
  -   accept: */*
  -   accept-encoding: gzip,deflate,br
  -   content-type: application/x-www-form-urlencoded; charset=UTF-8
  -   cookie: ASP.NET_SessionId={SESSION}; __RequestVerificationToken={XSRF}; .AspNet.ApplicationCookie={APP_COOKIE}
  -   content-length: 412
[2026-10-24 10:15:12] watch: polling again
"""

# The response half of a call log, upper case, with a set-cookie.
RESPONSE_LOG = f"""Page.goto: net::ERR_ABORTED
Call log:
  - navigating to "https://app.courtreserve.com/Online/Portal/Index/13234"
  - ← 302 Found
  -   Set-Cookie: .AspNetCore.Antiforgery.Q2x={CORE}; path=/; samesite=strict; httponly
  -   SET-COOKIE: ASP.NET_SessionId={SESSION}; path=/; HttpOnly
\t-\tLocation: /Online/Account/LogIn/13234
"""


def assert_clean(test, text):
    test.assertNotIn("SECRET", text)


class ScrubTests(unittest.TestCase):
    def test_playwright_request_call_log(self):
        out = scrub(CALL_LOG)
        assert_clean(self, out)
        self.assertIn(f"  -   cookie: {COOKIE_MARK}\n", out)
        # Everything else stays readable.
        self.assertIn("read ECONNRESET", out)
        self.assertIn("  -   user-agent: Mozilla/5.0", out)
        self.assertIn("  -   content-length: 412", out)
        self.assertIn("watch: polling again", out)
        self.assertEqual(len(out.splitlines()), len(CALL_LOG.splitlines()))

    def test_response_headers_in_any_case_and_indentation(self):
        out = scrub(RESPONSE_LOG)
        assert_clean(self, out)
        self.assertEqual(out.count(COOKIE_MARK), 2)
        self.assertIn("Location: /Online/Account/LogIn/13234", out)

    def test_authorization_headers(self):
        out = scrub(f"  -   Authorization: Bearer {BEARER}\n"
                    f"  -   proxy-authorization: Basic SECRETdXNlcjpwYXNz\n"
                    f"retrying with bearer {BEARER} next\n")
        assert_clean(self, out)
        self.assertEqual(out.count(AUTH_MARK), 3)

    def test_quoted_headers(self):
        out = scrub(f"""headers={{'Cookie': 'ASP.NET_SessionId={SESSION}', 'accept': '*/*'}}\n"""
                    f"""{{"cookie": "a=SECRETx\\"y", "authorization": "SECRETauth"}}""")
        assert_clean(self, out)
        self.assertIn("'accept': '*/*'", out)  # the rest of the dict survives

    def test_named_cookies_outside_a_header(self):
        out = scrub(f"POST body: __RequestVerificationToken_Lw__={XSRF}&Date=2026-10-24\n"
                    f"session ASP.NET_SessionId = {SESSION}, then\n"
                    f'{{"__RequestVerificationToken": "{XSRF}"}}')
        assert_clean(self, out)
        self.assertIn("&Date=2026-10-24", out)

    def test_storage_state_cookie_records(self):
        out = scrub(f'[{{"name": "ASP.NET_SessionId", "value": "{SESSION}", "domain": '
                    f'"app.courtreserve.com"}}, {{"name": "something_else", "value": "SECRETv"}}]')
        assert_clean(self, out)
        self.assertIn('"domain": "app.courtreserve.com"', out)

    def test_other_long_values_on_a_cookie_line(self):
        out = scrub("saving cookie jar: cf_clearance=SECRETcf1234567890abcdef path=/\n"
                    "unrelated=SECRETnot_a_crumb_but_long_enough\n")
        self.assertNotIn("SECRETcf", out)
        self.assertIn("path=/", out)
        # Only lines that mention cookies get the general rule.
        self.assertIn("unrelated=SECRETnot_a_crumb_but_long_enough", out)

    def test_ordinary_log_lines_are_untouched(self):
        text = ("[2026-10-24 12:00:00] watch: polling for 19:30 on 2026-10-31 (interval=30s)\n"
                "No slot yet; 3 courts at 18:00, none at 19:30\n"
                "HTTP 200 from ReadConsolidated was not JSON — title='Just a moment...'\n")
        self.assertEqual(scrub(text), text)

    def test_counts_per_rule(self):
        _, counts = scrub_counted(CALL_LOG + RESPONSE_LOG)
        self.assertEqual(counts, {"header_line": 3})

    def test_idempotent(self):
        once = scrub(CALL_LOG + RESPONSE_LOG + f'"cookie": "{SESSION}"')
        twice, counts = scrub_counted(once)
        self.assertEqual(twice, once)
        self.assertEqual(counts, {})

    def test_empty(self):
        self.assertEqual(scrub(""), "")
        self.assertEqual(scrub_counted(None), (None, {}))


class ScrubRunLogsScriptTests(unittest.TestCase):
    """The one-off cleanup, against a synthetic database shaped like the app's."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="cr-scrub-test-"))
        self.db = self.dir / "court_reserve.db"
        conn = sqlite3.connect(self.db)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE job_runs (id INTEGER PRIMARY KEY, job_id INTEGER, status TEXT,
                                   log_text TEXT DEFAULT '');
            CREATE TABLE pending_transfers (id INTEGER PRIMARY KEY, status TEXT,
                                            note TEXT DEFAULT '', log_text TEXT DEFAULT '');
        """)
        conn.executemany("INSERT INTO job_runs (id, job_id, status, log_text) VALUES (?, 1, ?, ?)",
                         [(1, "success", "Booking confirmed!\n"),
                          (2, "failed", CALL_LOG),
                          (3, "failed", RESPONSE_LOG),
                          (4, "running", None)])
        conn.execute("INSERT INTO pending_transfers VALUES (1, 'held_by_probe', ?, ?)",
                     (f"Transfer errored (cookie: ASP.NET_SessionId={SESSION})", CALL_LOG))
        conn.commit()
        conn.close()

    def run_script(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = scrub_run_logs.main(["--db", str(self.db), *args])
        return code, out.getvalue()

    def dump(self):
        conn = sqlite3.connect(self.db)
        try:
            text = "\n".join(r[0] or "" for r in conn.execute("SELECT log_text FROM job_runs"))
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'pending_transfers'").fetchone():
                text += "\n".join(f"{a}{b}" for a, b in
                                  conn.execute("SELECT note, log_text FROM pending_transfers"))
            return text
        finally:
            conn.close()

    def backups(self):
        return sorted(self.dir.glob("court_reserve.db.pre-scrub-*.bak"))

    def test_dry_run_prints_counts_only_and_writes_nothing(self):
        before = self.dump()
        code, out = self.run_script("--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(self.dump(), before)
        self.assertEqual(self.backups(), [])
        assert_clean(self, out)
        self.assertNotIn("ASP.NET", out)
        self.assertNotIn("ECONNRESET", out)  # no log text at all, not just no cookies
        self.assertIn("job_runs.log_text: 4 rows, 2 with something to remove", out)
        self.assertIn("pending_transfers.log_text: 1 rows, 1 with something to remove", out)
        self.assertIn("pending_transfers.note: 1 rows, 1 with something to remove", out)
        self.assertIn("header_line: 3", out)

    def test_scrubs_every_column_and_backs_up_first(self):
        before = self.dump()
        code, out = self.run_script()
        self.assertEqual(code, 0)
        assert_clean(self, out)
        after = self.dump()
        assert_clean(self, after)
        self.assertIn("Booking confirmed!", after)
        self.assertIn("read ECONNRESET", after)

        [bak] = self.backups()
        self.assertEqual(stat.S_IMODE(os.stat(bak).st_mode), 0o600)
        conn = sqlite3.connect(bak)
        try:
            [(n,)] = conn.execute("SELECT count(*) FROM job_runs")
            [(log,)] = conn.execute("SELECT log_text FROM job_runs WHERE id = 2")
        finally:
            conn.close()
        self.assertEqual(n, 4)
        self.assertIn(SESSION, log)  # the backup is the original, for a rollback
        self.assertIn(SESSION, before)

    def test_second_run_changes_nothing_and_makes_no_backup(self):
        self.run_script()
        after_first = self.dump()
        code, out = self.run_script()
        self.assertEqual(code, 0)
        self.assertEqual(self.dump(), after_first)
        self.assertEqual(len(self.backups()), 1)
        self.assertIn("Nothing to scrub", out)

    def test_a_failure_rolls_back_every_update(self):
        before = self.dump()
        real = scrub_run_logs.scan
        calls = []

        def scan_then_break(conn):
            report, updates = real(conn)
            calls.append(1)
            if len(calls) == 2:  # the rescan inside the transaction
                updates.append(("job_runs", "no_such_column", 1, "x"))
            return report, updates

        scrub_run_logs.scan = scan_then_break
        try:
            with self.assertRaises(sqlite3.OperationalError):
                self.run_script()
        finally:
            scrub_run_logs.scan = real
        self.assertEqual(self.dump(), before)

    def test_without_pending_transfers_table(self):
        conn = sqlite3.connect(self.db)
        conn.execute("DROP TABLE pending_transfers")
        conn.commit()
        conn.close()
        code, out = self.run_script()
        self.assertEqual(code, 0)
        self.assertNotIn("pending_transfers", out)
        assert_clean(self, self.dump())

    def test_missing_database(self):
        with redirect_stdout(io.StringIO()), \
                mock.patch("sys.stderr", io.StringIO()):
            code = scrub_run_logs.main(["--db", str(self.dir / "nope.db"), "--dry-run"])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
