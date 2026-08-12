"""Unit tests for the helpers in web/job_runner.py.

Importing web.job_runner creates a SQLite engine (via web.database), so we point
DATA_DIR at a throwaway temp dir *before* importing to avoid touching real data.
That same temp DB backs the run-lifecycle integration tests at the bottom.

Run:  python -m unittest test_job_runner -v
"""

import json
import os
import tempfile
import threading
import time
import unittest
from datetime import date, datetime
from unittest import mock

import pytz

# Must be set before importing web.job_runner -> web.database (reads DATA_DIR at import).
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cr-test-data-"))

from sqlalchemy import insert

from web.database import accounts, engine, init_db, job_runs, jobs, organizations
from booking import Slot, find_best_slot
import web.job_runner as jr
from web.job_runner import (_append_run_log, _deadline_timeout_minutes, _finish_run,
                            _is_network_error, _live_times, _notify_run_failed,
                            _parse_preferred_times, _send_warning_email, _start_run,
                            request_cancel, run_watch, SMTP_ATTEMPTS, SMTP_TIMEOUT_SECONDS)


class IsNetworkErrorTests(unittest.TestCase):
    def test_timeout_is_transient(self):
        self.assertTrue(_is_network_error(Exception("Page.wait_for_selector: Timeout 15000ms exceeded")))

    def test_dns_and_connection_errors_are_transient(self):
        self.assertTrue(_is_network_error(Exception("getaddrinfo ENOTFOUND")))
        self.assertTrue(_is_network_error(Exception("net::ERR_CONNECTION_REFUSED")))
        self.assertTrue(_is_network_error(Exception("net::ERR_NAME_NOT_RESOLVED")))

    def test_case_insensitive(self):
        self.assertTrue(_is_network_error(Exception("TIMED OUT")))

    def test_socket_hang_up_is_transient(self):
        # Run #260: connection dropped mid-request to ReadConsolidated.
        self.assertTrue(_is_network_error(Exception("APIRequestContext.post: socket hang up")))

    def test_connection_reset_and_bad_gateway_are_transient(self):
        self.assertTrue(_is_network_error(Exception("net::ERR_CONNECTION_RESET")))
        self.assertTrue(_is_network_error(Exception("502 Bad Gateway")))
        self.assertTrue(_is_network_error(Exception("503 Service Unavailable")))

    def test_raw_errno_codes_are_transient(self):
        # Run #282: libuv reports the bare errno with no separator, so it matches
        # neither "timeout" nor "timed out" — it killed a watch job 32h early.
        self.assertTrue(_is_network_error(Exception("APIRequestContext.post: read ETIMEDOUT")))
        self.assertTrue(_is_network_error(Exception("connect ECONNREFUSED 1.2.3.4:443")))
        self.assertTrue(_is_network_error(Exception("connect EHOSTUNREACH")))
        self.assertTrue(_is_network_error(Exception("connect ENETUNREACH")))
        self.assertTrue(_is_network_error(Exception("socket ECONNABORTED")))

    def test_cli_and_web_share_one_marker_list(self):
        # The two copies drifting apart is what let run #282 fail; they must be identical.
        import main
        import net_errors
        self.assertIs(main._is_network_error, net_errors.is_network_error)
        self.assertIs(_is_network_error, net_errors.is_network_error)

    def test_json_decode_error_is_transient(self):
        # Run #264: ReadConsolidated returned an HTML/Cloudflare page instead of JSON,
        # so APIResponse.json() raised json.JSONDecodeError ("Expecting value: ...").
        try:
            json.loads("<html>error</html>")
        except json.JSONDecodeError as e:
            self.assertTrue(_is_network_error(e))
        else:
            self.fail("expected JSONDecodeError")

    def test_booking_rejection_is_not_transient(self):
        self.assertFalse(_is_network_error(Exception("You are restricted to 1 reservation per day")))

    def test_plain_error_is_not_transient(self):
        self.assertFalse(_is_network_error(ValueError("bad value")))

    def test_real_bug_is_not_transient(self):
        # A genuine parsing bug (missing key) must still fail loudly, not be swallowed.
        self.assertFalse(_is_network_error(KeyError("CourtType")))


class ParsePreferredTimesTests(unittest.TestCase):
    def test_list_is_trimmed_and_emptied(self):
        self.assertEqual(_parse_preferred_times([" 18:00 ", "", "19:00"]), ["18:00", "19:00"])

    def test_comma_separated_string(self):
        self.assertEqual(_parse_preferred_times("18:00, 19:00"), ["18:00", "19:00"])

    def test_single_string(self):
        self.assertEqual(_parse_preferred_times("18:00"), ["18:00"])

    def test_empty_string(self):
        self.assertEqual(_parse_preferred_times(""), [])

    def test_none(self):
        self.assertEqual(_parse_preferred_times(None), [])


class MultiTimeWatchTests(unittest.TestCase):
    """A watch job may name several candidate start times in priority order. Each poll
    already fetches the whole day in one request, so extra times cost nothing -- but
    each carries its own deadline and they expire independently."""

    TZ = "America/Los_Angeles"

    def setUp(self):
        # Freeze "now" at 15:00 local on the target date so deadlines are deterministic.
        self.target = date(2026, 8, 17)
        self.now = pytz.timezone(self.TZ).localize(datetime(2026, 8, 17, 15, 0))
        patcher = mock.patch("web.job_runner.datetime", wraps=datetime)
        self.mock_dt = patcher.start()
        self.addCleanup(patcher.stop)
        self.mock_dt.now.return_value = self.now
        self.mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

    def test_single_time_still_works(self):
        # 19:00 court, 4h10m deadline -> cutoff 14:50, already past at 15:00.
        self.assertEqual(
            _deadline_timeout_minutes("4h10m", self.target, "19:00", self.TZ), 0)
        # 20:00 court -> cutoff 15:50, 50 minutes left.
        self.assertEqual(
            _deadline_timeout_minutes("4h10m", self.target, "20:00", self.TZ), 50)

    def test_multi_time_does_not_crash(self):
        # Regression: this raised ValueError("invalid literal for int()") because the
        # whole string was fed to int() via split(":").
        self.assertEqual(
            _deadline_timeout_minutes("4h10m", self.target, "19:00,19:30,20:00", self.TZ), 50)

    def test_multi_time_runs_until_the_last_deadline(self):
        # Job must not stop when the *earliest* time expires -- later ones are live.
        self.assertEqual(
            _deadline_timeout_minutes("4h10m", self.target, "19:00,20:00", self.TZ),
            _deadline_timeout_minutes("4h10m", self.target, "20:00", self.TZ))

    def test_expired_times_are_pruned_but_order_is_kept(self):
        live = _live_times("4h10m", self.target, ["19:00", "19:30", "20:00"], self.TZ)
        self.assertEqual(live, ["19:30", "20:00"])  # 19:00 cutoff 14:50 has passed

    def test_all_expired_yields_nothing_to_watch(self):
        self.assertEqual(_live_times("4h10m", self.target, ["19:00"], self.TZ), [])

    def test_infinite_never_prunes_or_times_out(self):
        times = ["19:00", "20:00"]
        self.assertEqual(_live_times("infinite", self.target, times, self.TZ), times)
        self.assertEqual(
            _deadline_timeout_minutes("infinite", self.target, "19:00,20:00", self.TZ), 0)

    def test_no_time_means_watch_anything(self):
        self.assertEqual(_deadline_timeout_minutes("4h10m", self.target, "", self.TZ), 0)
        self.assertEqual(_live_times("4h10m", self.target, [], self.TZ), [])

    def test_whitespace_in_the_list_is_tolerated(self):
        # The UI joins picker rows with "," but hand-edited params may have spaces.
        self.assertEqual(
            _deadline_timeout_minutes("4h10m", self.target, " 19:00 , 20:00 ", self.TZ), 50)


class MultiTimeSlotSelectionTests(unittest.TestCase):
    """find_best_slot already drives the choice; pin the behaviour run_watch relies on."""

    TZ = "America/Los_Angeles"

    def _slot(self, start_time, court_ids=(1,), waitlist=False):
        base = pytz.timezone(self.TZ).localize(
            datetime.strptime(f"2026-08-17 {start_time}", "%Y-%m-%d %H:%M"))
        return Slot(
            court_type="Hard",
            start_ms=int(base.timestamp() * 1000),
            end_ms=int(base.timestamp() * 1000) + 30 * 60 * 1000,
            available_courts=len(court_ids),
            available_court_ids=list(court_ids),
            is_wait_list=waitlist,
            timezone=self.TZ,
        )

    def test_earliest_listed_preference_wins(self):
        slots = [self._slot("19:30"), self._slot("20:00")]
        self.assertEqual(find_best_slot(slots, ["20:00", "19:30"], allow_fallback=False).start_time,
                         "20:00")
        self.assertEqual(find_best_slot(slots, ["19:30", "20:00"], allow_fallback=False).start_time,
                         "19:30")

    def test_falls_through_to_the_next_listed_time(self):
        slots = [self._slot("20:00")]
        self.assertEqual(
            find_best_slot(slots, ["19:00", "19:30", "20:00"], allow_fallback=False).start_time,
            "20:00")

    def test_never_books_an_unlisted_time(self):
        # allow_fallback=False is what stops a targeted watch grabbing a random slot.
        slots = [self._slot("18:00"), self._slot("21:00")]
        self.assertIsNone(find_best_slot(slots, ["19:00", "19:30"], allow_fallback=False))

    def test_waitlist_slots_are_not_matches(self):
        slots = [self._slot("19:00", waitlist=True), self._slot("19:30")]
        self.assertEqual(find_best_slot(slots, ["19:00", "19:30"], allow_fallback=False).start_time,
                         "19:30")


class CancellationRegistryTests(unittest.TestCase):
    """APScheduler's remove_job only drops the trigger, so a watch already mid-poll
    outlived edit/pause/delete: still holding a browser, still able to book for a job
    that no longer existed. Runs now publish an Event that callers can set."""

    def tearDown(self):
        with jr._cancel_lock:
            jr._cancel_events.clear()

    def test_cancel_sets_the_running_events_flag(self):
        event = jr._register_cancel(42)
        self.assertFalse(event.is_set())
        self.assertTrue(request_cancel(42))
        self.assertTrue(event.is_set())

    def test_cancelling_an_idle_job_is_a_no_op(self):
        # Edit/delete call this unconditionally; jobs that aren't running are common.
        self.assertFalse(request_cancel(999))

    def test_unregister_removes_the_event(self):
        event = jr._register_cancel(42)
        jr._unregister_cancel(42, event)
        self.assertFalse(request_cancel(42))

    def test_a_finishing_run_cannot_retract_its_replacements_event(self):
        # The edit path cancels the old run and immediately schedules a new one. If the
        # old thread's cleanup deleted whatever was registered, the *new* run would be
        # silently uncancellable -- reintroducing the bug for every subsequent edit.
        old = jr._register_cancel(42)
        request_cancel(42)
        new = jr._register_cancel(42)          # replacement run starts
        jr._unregister_cancel(42, old)         # old thread finally-block lands late
        self.assertFalse(new.is_set())
        self.assertTrue(request_cancel(42))
        self.assertTrue(new.is_set())

    def test_each_job_is_isolated(self):
        a, b = jr._register_cancel(1), jr._register_cancel(2)
        request_cancel(1)
        self.assertTrue(a.is_set())
        self.assertFalse(b.is_set())

    def test_cancel_wakes_a_waiting_thread_promptly(self):
        # The poll loop waits on the Event rather than sleeping, so a cancel lands at
        # once instead of after a full interval (60s in production).
        event = jr._register_cancel(42)
        result = {}

        def worker():
            t0 = time.monotonic()
            result["cancelled"] = event.wait(30)
            result["elapsed"] = time.monotonic() - t0

        t = threading.Thread(target=worker)
        t.start()
        time.sleep(0.05)
        request_cancel(42)
        t.join(timeout=5)
        self.assertTrue(result["cancelled"])
        self.assertLess(result["elapsed"], 2.0, "cancel should not wait out the interval")

    def test_cancelled_runs_do_not_send_failure_email(self):
        # "cancelled" must not be spelled "failed": editing a job would then fire a
        # spurious "Job failed" alert every time.
        run_id = _start_run(_cancel_job_id())
        with mock.patch("smtplib.SMTP") as smtp:
            _finish_run(run_id, "cancelled", "watch: cancelled\n")
            smtp.assert_not_called()
        with engine.connect() as conn:
            row = conn.execute(job_runs.select().where(job_runs.c.id == run_id)).fetchone()
        self.assertEqual(row.status, "cancelled")
        self.assertNotIn("failure notification", row.log_text)


class RunWatchCancelPathTests(unittest.TestCase):
    """Drive the real run_watch cancel path. Asserting on _finish_run alone is not
    enough: it leaves run_watch free to report a cancellation as "failed", which would
    fire a spurious "Job failed" email on every edit."""

    def setUp(self):
        self.job_id = _cancel_job_id()
        with engine.begin() as conn:
            conn.execute(jobs.update().where(jobs.c.id == self.job_id).values(status="active"))
        # Enter the poll loop already cancelled, as if an edit landed a moment earlier.
        preset = threading.Event()
        preset.set()
        for target, new in [("web.job_runner._register_cancel", lambda _job_id: preset),
                            ("web.job_runner.ensure_logged_in", mock.DEFAULT),
                            ("web.job_runner.wait_until", mock.DEFAULT)]:
            p = mock.patch(target, new) if new is not mock.DEFAULT else mock.patch(target)
            p.start()
            self.addCleanup(p.stop)
        pw = mock.patch("web.job_runner.sync_playwright")
        self.pw = pw.start()
        self.addCleanup(pw.stop)
        self.browser = self.pw.return_value.__enter__.return_value.chromium.launch.return_value

    def _run(self):
        with mock.patch("smtplib.SMTP") as smtp:
            run_watch(self.job_id, _cancel_account_id(), "2026-08-17", "19:00",
                      duration=120, interval=60, deadline_mode="infinite")
        return smtp

    def test_cancelled_before_first_poll_ends_as_cancelled(self):
        smtp = self._run()
        with engine.connect() as conn:
            run = conn.execute(job_runs.select().where(job_runs.c.job_id == self.job_id)
                               .order_by(job_runs.c.id.desc())).fetchone()
        self.assertEqual(run.status, "cancelled")
        self.assertIn("Cancelled", run.log_text)
        smtp.assert_not_called()          # never alert on a deliberate cancellation

    def test_browser_is_closed_on_cancel(self):
        # Otherwise cancelling leaks the chromium processes it was holding.
        self._run()
        self.browser.close.assert_called()

    def test_cancel_does_not_clobber_the_jobs_row(self):
        # An edit sets status=active and schedules a replacement; the retiring thread
        # must not then stamp "failed" over it.
        self._run()
        with engine.connect() as conn:
            job = conn.execute(jobs.select().where(jobs.c.id == self.job_id)).fetchone()
        self.assertEqual(job.status, "active")

    def test_the_event_is_retracted_afterwards(self):
        self._run()
        self.assertFalse(request_cancel(self.job_id))


def _cancel_account_id():
    with engine.connect() as conn:
        return conn.execute(accounts.select().order_by(accounts.c.id.desc())).fetchone().id


def _cancel_job_id():
    """A throwaway job row for run-lifecycle assertions."""
    init_db()
    with engine.begin() as conn:
        org_id = conn.execute(insert(organizations).values(
            name="Cancel Org", org_id="1", scheduler_id="1", cost_type_id="1",
            timezone="America/Los_Angeles")).inserted_primary_key[0]
        acct_id = conn.execute(insert(accounts).values(
            label="c", email="c@example.com", password="p")).inserted_primary_key[0]
        return conn.execute(insert(jobs).values(
            account_id=acct_id, org_id=org_id, type="watch", params="{}")).inserted_primary_key[0]


class SendWarningEmailTests(unittest.TestCase):
    """Run #282 got no alert because the SMTP send could block forever and its only
    error report was a print() nobody sees. Pin down the timeout, the retry policy,
    and the returned outcome string that now lands in the run log."""

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {
            "NOTIFY_EMAIL": "to@example.com",
            "SMTP_PASSWORD": "hunter2",
            "SMTP_USER": "from@example.com",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        # Never actually wait between retries.
        sleep = mock.patch("web.job_runner.time.sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def test_no_config_is_reported_not_attempted(self):
        with mock.patch.dict(os.environ, {"NOTIFY_EMAIL": "", "SMTP_PASSWORD": ""}):
            with mock.patch("smtplib.SMTP") as smtp:
                self.assertIn("not configured", _send_warning_email("s", "b"))
                smtp.assert_not_called()

    def test_success_reports_sent_and_bounds_the_connection(self):
        with mock.patch("smtplib.SMTP") as smtp:
            self.assertEqual(_send_warning_email("s", "b"), "sent")
        # A missing timeout is what turns an outage into a silent hang.
        self.assertEqual(smtp.call_args.kwargs.get("timeout"), SMTP_TIMEOUT_SECONDS)

    def test_network_failure_is_retried_then_succeeds(self):
        with mock.patch("smtplib.SMTP") as smtp:
            smtp.side_effect = [TimeoutError("read ETIMEDOUT"), mock.DEFAULT]
            smtp.return_value.__enter__ = mock.Mock(return_value=mock.Mock())
            smtp.return_value.__exit__ = mock.Mock(return_value=False)
            outcome = _send_warning_email("s", "b")
        self.assertEqual(smtp.call_count, 2)
        self.assertTrue(outcome.startswith("sent"), outcome)

    def test_persistent_network_failure_reports_failure(self):
        with mock.patch("smtplib.SMTP", side_effect=TimeoutError("read ETIMEDOUT")) as smtp:
            outcome = _send_warning_email("s", "b")
        self.assertEqual(smtp.call_count, SMTP_ATTEMPTS)
        self.assertTrue(outcome.startswith("failed:"), outcome)
        self.assertIn("ETIMEDOUT", outcome)

    def test_auth_failure_is_not_retried(self):
        # A bad password never recovers; retrying just delays the run's completion.
        import smtplib as _smtplib
        err = _smtplib.SMTPAuthenticationError(535, b"bad credentials")
        with mock.patch("smtplib.SMTP", side_effect=err) as smtp:
            outcome = _send_warning_email("s", "b")
        self.assertEqual(smtp.call_count, 1)
        self.assertTrue(outcome.startswith("failed:"), outcome)

    def test_send_never_raises_into_the_caller(self):
        # _finish_run calls this on the failure path; it must not mask the real error.
        with mock.patch("smtplib.SMTP", side_effect=RuntimeError("boom")):
            self.assertTrue(_send_warning_email("s", "b").startswith("failed:"))


class RunFailureNotificationTests(unittest.TestCase):
    """Integration coverage for _finish_run -> _notify_run_failed -> _append_run_log,
    against the real (temp) SQLite DB.

    Run #282 failed and no alert arrived, and the run page gave no hint that a
    notification had even been attempted: the send's only error report was a print()
    issued after the run's stdout had already been snapshotted into the DB. These
    tests assert the outcome is persisted where you'd actually look for it.
    """

    @classmethod
    def setUpClass(cls):
        init_db()
        with engine.begin() as conn:
            cls.org_id = conn.execute(insert(organizations).values(
                name="Test Org", org_id="13234", scheduler_id="16995",
                cost_type_id="1", timezone="America/Los_Angeles",
            )).inserted_primary_key[0]
            cls.account_id = conn.execute(insert(accounts).values(
                label="Tester", email="tester@example.com", password="pw",
            )).inserted_primary_key[0]

    def setUp(self):
        with engine.begin() as conn:
            self.job_id = conn.execute(insert(jobs).values(
                account_id=self.account_id, org_id=self.org_id, type="watch",
                params='{"date": "2026-08-09", "time": "19:30"}',
            )).inserted_primary_key[0]
        self.env = mock.patch.dict(os.environ, {
            "NOTIFY_EMAIL": "to@example.com",
            "SMTP_PASSWORD": "hunter2",
            "SMTP_USER": "from@example.com",
            "WEB_BASE_URL": "http://tennis.example.com",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        sleep = mock.patch("web.job_runner.time.sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def _read_run(self, run_id):
        with engine.connect() as conn:
            return conn.execute(job_runs.select().where(job_runs.c.id == run_id)).fetchone()

    def _sent_body(self, smtp):
        """Extract the message text handed to sendmail()."""
        session = smtp.return_value.__enter__.return_value
        return session.sendmail.call_args.args[2]

    def test_failed_run_records_that_the_alert_was_sent(self):
        run_id = _start_run(self.job_id)
        with mock.patch("smtplib.SMTP") as smtp:
            _finish_run(run_id, "failed", "poll line\nERROR: read ETIMEDOUT\n")
        run = self._read_run(run_id)
        self.assertEqual(run.status, "failed")
        self.assertIsNotNone(run.finished_at)
        # The original log survives, with the notification outcome appended.
        self.assertIn("ERROR: read ETIMEDOUT", run.log_text)
        self.assertTrue(run.log_text.rstrip().endswith("[failure notification] sent"),
                        run.log_text)

    def test_alert_body_carries_the_run_link_and_log_tail(self):
        run_id = _start_run(self.job_id)
        with mock.patch("smtplib.SMTP") as smtp:
            _finish_run(run_id, "failed", "poll line\nERROR: read ETIMEDOUT\n")
            body = self._sent_body(smtp)
        self.assertIn(f"http://tennis.example.com/runs/{run_id}", body)
        self.assertIn("ERROR: read ETIMEDOUT", body)
        self.assertIn(f"#{self.job_id}", body)
        self.assertIn("2026-08-09", body)  # job params, so you can see what was lost

    def test_unsendable_alert_is_recorded_on_the_run(self):
        # The exact run #282 scenario: the network that killed the run also kills the
        # alert. Previously this left no trace anywhere the web UI could show.
        run_id = _start_run(self.job_id)
        with mock.patch("smtplib.SMTP", side_effect=TimeoutError("read ETIMEDOUT")):
            _finish_run(run_id, "failed", "ERROR: read ETIMEDOUT\n")
        log = self._read_run(run_id).log_text
        self.assertIn("[failure notification] failed:", log)
        self.assertIn("TimeoutError", log)

    def test_unconfigured_smtp_is_recorded_not_silent(self):
        run_id = _start_run(self.job_id)
        with mock.patch.dict(os.environ, {"NOTIFY_EMAIL": "", "SMTP_PASSWORD": ""}):
            with mock.patch("smtplib.SMTP") as smtp:
                _finish_run(run_id, "failed", "ERROR: boom\n")
                smtp.assert_not_called()
        self.assertIn("[failure notification] not configured",
                      self._read_run(run_id).log_text)

    def test_successful_run_does_not_notify(self):
        run_id = _start_run(self.job_id)
        with mock.patch("smtplib.SMTP") as smtp:
            _finish_run(run_id, "success", "Booking confirmed!\n")
            smtp.assert_not_called()
        log = self._read_run(run_id).log_text
        self.assertNotIn("failure notification", log)
        self.assertEqual(log, "Booking confirmed!\n")

    def test_intermediate_running_updates_do_not_notify(self):
        # run_watch calls _finish_run(status="running") on every poll — for a job that
        # polls for days, notifying there would mean thousands of emails.
        run_id = _start_run(self.job_id)
        with mock.patch("smtplib.SMTP") as smtp:
            _finish_run(run_id, "running", "poll 1\n")
            _finish_run(run_id, "running", "poll 1\npoll 2\n")
            smtp.assert_not_called()
        self.assertEqual(self._read_run(run_id).log_text, "poll 1\npoll 2\n")

    def test_notification_failure_never_propagates(self):
        # _finish_run runs on the way out of a failed job; a broken notifier must not
        # replace the real error with its own.
        run_id = _start_run(self.job_id)
        with mock.patch("web.job_runner._send_warning_email",
                        side_effect=RuntimeError("notifier exploded")):
            _finish_run(run_id, "failed", "ERROR: the real problem\n")
        run = self._read_run(run_id)
        self.assertEqual(run.status, "failed")
        self.assertIn("ERROR: the real problem", run.log_text)
        self.assertIn("[failure notification] errored before sending", run.log_text)

    def test_append_run_log_is_additive(self):
        run_id = _start_run(self.job_id)
        _append_run_log(run_id, "first")
        _append_run_log(run_id, "second")
        self.assertEqual(self._read_run(run_id).log_text, "\nfirst\nsecond")

    def test_append_to_missing_run_is_a_no_op(self):
        _append_run_log(999_999, "orphan")  # must not raise

    def test_append_swallows_db_errors(self):
        # Appending is a diagnostic nicety; a locked/broken DB must not take down the
        # job runner on its way out of an already-failed run.
        with mock.patch("web.job_runner.engine.begin", side_effect=RuntimeError("db is locked")):
            _append_run_log(1, "note")  # must not raise

    def test_notify_for_unknown_run_is_a_no_op(self):
        with mock.patch("smtplib.SMTP") as smtp:
            _notify_run_failed(999_999, "log")
            smtp.assert_not_called()


if __name__ == "__main__":
    unittest.main()
