"""Tests for the home agent API (web/routes/agent_api.py).

Requests go through the real router, auth dependency and validation, driven over ASGI
directly (no httpx needed). The scheduler, the job runner's cancel hook and Playwright
are mocked: nothing here starts a job or reaches CourtReserve.

Run:  python -m unittest test_agent_api -v
"""

import asyncio
import hashlib
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

import pytz

# Must be set before importing web.database (reads DATA_DIR at import).
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cr-test-data-"))

from fastapi import FastAPI
from sqlalchemy import insert

from web.database import (account_orgs, accounts, bookings, engine, init_db, job_runs, jobs,
                          organizations, pending_transfers)
import web.job_runner as jr
from web.database import row_to_dict
from web.routes import agent_api

#: Saved before any test patches it, for the tests that need the real thing.
REAL_REQUEST_CANCEL = jr.request_cancel

TOKEN = "cr_agent_test-token"
PASSWORD = "Sup3rS3cretCRpw"  # a fixture value that must never come back
EMAIL = "player@example.com"
TZ = pytz.timezone("America/Los_Angeles")

app = FastAPI()
app.include_router(agent_api.router)


def call(method, path, body=None, token=TOKEN):
    """One request through the ASGI app; returns (status, parsed JSON)."""
    async def run():
        sent = []
        payload = json.dumps(body).encode() if body is not None else b""
        messages = [{"type": "http.request", "body": payload, "more_body": False}]

        async def receive():
            return messages.pop(0) if messages else {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        p, _, qs = path.partition("?")
        headers = [(b"content-type", b"application/json")]
        if token is not None:
            headers.append((b"authorization", f"Bearer {token}".encode()))
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": method, "scheme": "http", "path": p, "raw_path": p.encode(),
                 "query_string": qs.encode(), "root_path": "", "headers": headers,
                 "client": ("test", 1), "server": ("test", 80)}
        await app(scope, receive, send)
        status = next(m["status"] for m in sent if m["type"] == "http.response.start")
        raw = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        return status, raw.decode()

    status, raw = asyncio.run(run())
    return status, (json.loads(raw) if raw else None), raw


def future(days=10):
    return (datetime.now(TZ).date() + timedelta(days=days)).isoformat()


class AgentApiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        with engine.begin() as conn:
            cls.org_id = conn.execute(insert(organizations).values(
                name="Test Club", org_id="99999", scheduler_id="1", cost_type_id="2",
                timezone="America/Los_Angeles", resident_days_out=8, nonresident_days_out=7,
                release_hour=12, release_minute=0)).inserted_primary_key[0]
            cls.acc_id = conn.execute(insert(accounts).values(
                label="", email=EMAIL, password=PASSWORD, session_file="")).inserted_primary_key[0]
            cls.link_id = conn.execute(insert(account_orgs).values(
                account_id=cls.acc_id, org_id=cls.org_id, is_resident=True)).inserted_primary_key[0]
            # A club nobody is linked to, and an account linked to nothing.
            cls.lonely_org_id = conn.execute(insert(organizations).values(
                name="Other Club", org_id="99998", scheduler_id="1", cost_type_id="2",
                timezone="America/Los_Angeles")).inserted_primary_key[0]
            cls.stray_acc_id = conn.execute(insert(accounts).values(
                label="Stray", email="stray@example.com", password="StrayPw999",
                session_file="")).inserted_primary_key[0]

    @classmethod
    def tearDownClass(cls):
        with engine.begin() as conn:
            conn.execute(account_orgs.delete().where(account_orgs.c.id == cls.link_id))
            conn.execute(accounts.delete().where(accounts.c.id.in_([cls.acc_id, cls.stray_acc_id])))
            conn.execute(organizations.delete().where(
                organizations.c.id.in_([cls.org_id, cls.lonely_org_id])))

    def setUp(self):
        env = mock.patch.dict(os.environ, {
            agent_api.TOKEN_HASH_ENV: hashlib.sha256(TOKEN.encode()).hexdigest()})
        env.start()
        self.addCleanup(env.stop)
        self.sched = {}
        for name in ("schedule_book_next", "schedule_watch", "pause_job", "resume_job"):
            p = mock.patch(f"web.apscheduler_setup.{name}")
            self.sched[name] = p.start()
            self.addCleanup(p.stop)
        p = mock.patch("web.job_runner.request_cancel")
        self.request_cancel = p.start()
        self.addCleanup(p.stop)
        self.job_ids = []
        self.addCleanup(self._drop_jobs)

    def _drop_jobs(self):
        with engine.begin() as conn:
            for jid in self.job_ids:
                run_ids = [r.id for r in conn.execute(job_runs.select().where(job_runs.c.job_id == jid))]
                if run_ids:
                    conn.execute(bookings.delete().where(bookings.c.job_run_id.in_(run_ids)))
                conn.execute(job_runs.delete().where(job_runs.c.job_id == jid))
                conn.execute(jobs.delete().where(jobs.c.id == jid))
            conn.execute(bookings.delete().where(
                (bookings.c.account_id == self.acc_id) & bookings.c.job_run_id.is_(None)))

    def add_run(self, job_id, status="running", log_text=""):
        with engine.begin() as conn:
            return conn.execute(insert(job_runs).values(
                job_id=job_id, status=status, log_text=log_text)).inserted_primary_key[0]

    @staticmethod
    def set_account(acc_id, **values):
        with engine.begin() as conn:
            conn.execute(accounts.update().where(accounts.c.id == acc_id).values(**values))

    def job_status(self, job_id):
        with engine.connect() as conn:
            return conn.execute(jobs.select().where(jobs.c.id == job_id)).fetchone().status

    def add_job(self, type_="watch", status="active", params=None):
        params = params or {"date": future(), "time": "19:00", "duration": 120,
                            "deadline_mode": "4h10m", "run_at": ""}
        with engine.begin() as conn:
            jid = conn.execute(insert(jobs).values(
                account_id=self.acc_id, org_id=self.org_id, type=type_,
                params=json.dumps(params), status=status)).inserted_primary_key[0]
        self.job_ids.append(jid)
        return jid

    def booking(self, **over):
        body = {"org_id": self.org_id, "account_id": self.acc_id, "date": future(),
                "times": ["19:00", "19:30"], "duration_min": 120}
        body.update(over)
        return body

    def created(self, out):
        self.job_ids.append(out["id"])
        return out


class AuthTests(AgentApiTestCase):
    def test_no_token_is_refused(self):
        self.assertEqual(call("GET", "/api/agent/v1/status", token=None)[0], 401)

    def test_a_wrong_token_is_refused(self):
        self.assertEqual(call("GET", "/api/agent/v1/status", token="cr_agent_guess")[0], 401)

    def test_the_hash_itself_is_not_a_token(self):
        digest = hashlib.sha256(TOKEN.encode()).hexdigest()
        self.assertEqual(call("GET", "/api/agent/v1/status", token=digest)[0], 401)

    def test_unconfigured_means_off(self):
        with mock.patch.dict(os.environ, {agent_api.TOKEN_HASH_ENV: ""}):
            self.assertEqual(call("GET", "/api/agent/v1/status")[0], 503)

    def test_writes_need_the_token_too(self):
        status, _, _ = call("POST", "/api/agent/v1/jobs/booking", self.booking(), token=None)
        self.assertEqual(status, 401)
        self.sched["schedule_book_next"].assert_not_called()

    def test_the_right_token_reads_status(self):
        status, body, _ = call("GET", "/api/agent/v1/status")
        self.assertEqual(status, 200)
        self.assertEqual(body["app"], "court-reserve")


class NoSecretsTests(AgentApiTestCase):
    def test_reads_never_carry_the_password_or_email(self):
        jid = self.add_job()
        for path in ("/status", "/clubs", "/jobs?state=all", f"/jobs/{jid}", "/reservations"):
            status, _, raw = call("GET", "/api/agent/v1" + path)
            self.assertEqual(status, 200, path)
            self.assertNotIn(PASSWORD, raw, path)
            self.assertNotIn(EMAIL, raw, path)

    def test_an_unlabelled_account_is_shown_masked(self):
        _, body, _ = call("GET", "/api/agent/v1/clubs")
        club = next(c for c in body["clubs"] if c["id"] == self.org_id)
        self.assertEqual(club["accounts"], [
            {"id": self.acc_id, "label": "p***@example.com", "resident": True, "days_out": 8}])

    def test_run_logs_are_scrubbed(self):
        jid = self.add_job()
        with engine.begin() as conn:
            conn.execute(insert(job_runs).values(
                job_id=jid, status="failed",
                log_text=f"Logging in as {EMAIL}...\nfill failed for {PASSWORD}\nERROR: boom"))
        with mock.patch.dict(os.environ, {"SMTP_PASSWORD": "smtp-app-pw"}):
            with engine.begin() as conn:
                conn.execute(insert(job_runs).values(job_id=jid, status="failed",
                                                     log_text="smtp login smtp-app-pw refused"))
            _, body, raw = call("GET", f"/api/agent/v1/jobs/{jid}")
        self.assertNotIn(PASSWORD, raw)
        self.assertNotIn("smtp-app-pw", raw)
        older = body["runs"][1]["log_tail"]
        self.assertIn("Logging in as p***@example.com", older)
        self.assertIn("[redacted]", older)

    def test_an_availability_error_is_scrubbed(self):
        with mock.patch.object(agent_api, "_fetch_slots",
                               side_effect=Exception(f"Timeout typing {PASSWORD}")):
            status, _, raw = call("GET", f"/api/agent/v1/availability?org_id={self.org_id}&date={future()}")
        self.assertEqual(status, 502)
        self.assertNotIn(PASSWORD, raw)

    def test_a_refused_login_names_the_account_not_its_email(self):
        with mock.patch.object(agent_api, "_fetch_slots",
                               side_effect=RuntimeError("Login failed — check email and password")):
            status, body, raw = call("GET", f"/api/agent/v1/availability?org_id={self.org_id}&date={future()}")
        self.assertEqual(status, 502)
        self.assertIn("refused the login for p***@example.com", body["detail"])

    def test_sync_errors_are_scrubbed(self):
        result = {"added": 0, "updated": 0, "removed": 0,
                  "errors": [f"{EMAIL}: bad password {PASSWORD}"]}
        with mock.patch("web.routes.reservations._do_sync", return_value=result):
            status, body, raw = call("POST", "/api/agent/v1/reservations/sync")
        self.assertEqual(status, 200)
        self.assertNotIn(PASSWORD, raw)
        self.assertNotIn(EMAIL, raw)


class ReadTests(AgentApiTestCase):
    def test_jobs_filter_by_state(self):
        active = self.add_job(status="active")
        done = self.add_job(type_="book_next", status="completed")
        current = {j["id"] for j in call("GET", "/api/agent/v1/jobs")[1]["jobs"]}
        finished = {j["id"] for j in call("GET", "/api/agent/v1/jobs?state=finished")[1]["jobs"]}
        self.assertIn(active, current)
        self.assertNotIn(done, current)
        self.assertIn(done, finished)
        self.assertEqual(call("GET", "/api/agent/v1/jobs?state=bogus")[0], 400)

    def test_a_job_says_what_it_booked(self):
        jid = self.add_job(type_="book_next", status="completed",
                           params={"date": future(3), "time": "18:00,18:30", "duration": 90})
        with engine.begin() as conn:
            rid = conn.execute(insert(job_runs).values(job_id=jid, status="success",
                                                       log_text="ok")).inserted_primary_key[0]
            conn.execute(insert(bookings).values(job_run_id=rid, account_id=self.acc_id,
                                                 date=future(3), start_time="18:30",
                                                 court_type="Hard", duration_min=90))
        _, j, _ = call("GET", f"/api/agent/v1/jobs/{jid}")
        self.assertEqual(j["kind"], "booking")
        self.assertEqual(j["times"], ["18:00", "18:30"])
        self.assertEqual(j["booked"], [{"date": future(3), "start_time": "18:30",
                                        "duration_min": 90, "court_type": "Hard"}])
        self.assertEqual(j["last_run"]["status"], "success")
        _, res, _ = call("GET", "/api/agent/v1/reservations")
        mine = [r for r in res["reservations"] if r["source"] == f"job {jid}"]
        self.assertEqual(mine[0]["club"], "Test Club")

    def test_an_unknown_job_is_404(self):
        self.assertEqual(call("GET", "/api/agent/v1/jobs/987654")[0], 404)

    def test_availability_returns_the_slots(self):
        slots = [{"start_time": "19:00", "court_type": "Hard", "available_courts": 2, "waitlist": False}]
        with mock.patch.object(agent_api, "_fetch_slots", return_value=slots) as fetch:
            status, body, _ = call("GET", f"/api/agent/v1/availability?org_id={self.org_id}&date={future()}")
        self.assertEqual(status, 200)
        self.assertEqual(body["slots"], slots)
        self.assertEqual(fetch.call_args.args[1]["id"], self.acc_id)

    def test_only_one_browser_at_a_time(self):
        self.assertTrue(agent_api._browser_lock.acquire(blocking=False))
        try:
            with mock.patch.object(agent_api, "_fetch_slots") as fetch:
                status, _, _ = call("GET", f"/api/agent/v1/availability?org_id={self.org_id}&date={future()}")
            self.assertEqual(status, 409)
            fetch.assert_not_called()
        finally:
            agent_api._browser_lock.release()


class CreateTests(AgentApiTestCase):
    def test_a_booking_job_is_created_and_scheduled(self):
        status, out, _ = call("POST", "/api/agent/v1/jobs/booking", self.booking())
        self.assertEqual(status, 200, out)
        out = self.created(out)
        self.assertEqual(out["kind"], "booking")
        self.assertEqual(out["status"], "active")
        params = self.sched["schedule_book_next"].call_args.args[2]
        self.assertEqual(params["time"], "19:00,19:30")
        self.assertEqual(params["duration"], 120)
        # Ten days out with an 8-day window: it waits for the window, 2 min early.
        self.assertTrue(params["run_at"].endswith("T11:58"))
        self.assertIn("before the booking window opens", out["fires"])

    def test_a_booking_inside_the_window_fires_now(self):
        out = self.created(call("POST", "/api/agent/v1/jobs/booking", self.booking(date=future(2)))[1])
        self.assertEqual(out["fires"], "now: the booking window is already open")

    def test_a_job_with_no_times_is_refused(self):
        # With none, the runner would book whatever slot it found.
        status, _, _ = call("POST", "/api/agent/v1/jobs/booking", self.booking(times=[]))
        self.assertEqual(status, 400)
        self.sched["schedule_book_next"].assert_not_called()

    def test_bad_inputs_are_refused(self):
        for over in ({"times": ["7pm"]}, {"times": ["19:15"]}, {"duration_min": 45},
                     {"date": "2020-01-01"}, {"date": future(90)}, {"date": "soon"},
                     {"org_id": 987654}, {"account_id": 987654}):
            status, _, _ = call("POST", "/api/agent/v1/jobs/booking", self.booking(**over))
            self.assertIn(status, (400, 404), over)
        self.sched["schedule_book_next"].assert_not_called()

    def test_options_the_api_does_not_offer_are_refused(self):
        # No probe accounts, auto-transfer or fire time from the agent.
        for extra in ({"probe_account_id": self.acc_id}, {"run_at": "2026-01-01T00:00"}):
            status, _, _ = call("POST", "/api/agent/v1/jobs/watch", self.booking(**extra))
            self.assertEqual(status, 422, extra)
        self.sched["schedule_watch"].assert_not_called()

    def test_a_second_job_for_the_same_day_is_refused(self):
        first = self.created(call("POST", "/api/agent/v1/jobs/booking", self.booking())[1])
        status, body, _ = call("POST", "/api/agent/v1/jobs/booking", self.booking(times=["08:00"]))
        self.assertEqual(status, 409)
        self.assertIn(f"Job {first['id']}", body["detail"])

    def test_a_watch_job_is_created_with_the_safe_defaults(self):
        status, out, _ = call("POST", "/api/agent/v1/jobs/watch", self.booking())
        self.assertEqual(status, 200, out)
        self.created(out)
        params = self.sched["schedule_watch"].call_args.args[2]
        self.assertEqual(params["deadline_mode"], "4h10m")
        self.assertEqual(params["interval"], 60)
        self.assertIsNone(params["probe_account_id"])
        self.assertFalse(params["auto_transfer"])

    def test_a_watch_without_a_deadline_is_refused(self):
        status, _, _ = call("POST", "/api/agent/v1/jobs/watch", self.booking(stop_before="infinite"))
        self.assertEqual(status, 400)


class PauseResumeTests(AgentApiTestCase):
    def test_pause_stops_the_running_thread_and_resume_rearms(self):
        jid = self.add_job(status="active")
        status, out, _ = call("POST", f"/api/agent/v1/jobs/{jid}/pause")
        self.assertEqual((status, out["status"]), (200, "paused"))
        self.request_cancel.assert_called_once_with(jid)
        status, out, _ = call("POST", f"/api/agent/v1/jobs/{jid}/resume")
        self.assertEqual((status, out["status"]), (200, "active"))
        # A one-shot watch's trigger is spent; resume must put a run back.
        self.sched["schedule_watch"].assert_called_once()

    def test_only_the_right_state_moves(self):
        done = self.add_job(status="completed")
        self.assertEqual(call("POST", f"/api/agent/v1/jobs/{done}/pause")[0], 409)
        self.assertEqual(call("POST", f"/api/agent/v1/jobs/{done}/resume")[0], 409)
        active = self.add_job(status="active")
        self.assertEqual(call("POST", f"/api/agent/v1/jobs/{active}/resume")[0], 409)

    def test_a_job_for_a_past_day_is_not_resumed(self):
        jid = self.add_job(status="paused", params={"date": "2020-01-01", "time": "19:00"})
        self.assertEqual(call("POST", f"/api/agent/v1/jobs/{jid}/resume")[0], 409)
        self.sched["resume_job"].assert_not_called()

    def test_there_is_no_delete_edit_or_run_now(self):
        jid = self.add_job()
        for path in (f"/jobs/{jid}/delete", f"/jobs/{jid}/run_now", f"/jobs/{jid}/restart",
                     f"/jobs/{jid}/edit", "/transfers/1/execute", "/accounts"):
            self.assertIn(call("POST", "/api/agent/v1" + path)[0], (404, 405), path)


class ReviewFixTests(AgentApiTestCase):
    """The fixes from the review of court-reserve#4, one or more per finding."""

    # 1. pause must never claim to have stopped a run that can still book.

    def test_pausing_a_booking_job_mid_run_is_refused_and_changes_nothing(self):
        jid = self.add_job(type_="book_next", status="active",
                           params={"date": future(), "time": "19:00", "duration": 120})
        self.add_run(jid, "running")
        status, body, _ = call("POST", f"/api/agent/v1/jobs/{jid}/pause")
        self.assertEqual(status, 409)
        self.assertIn("booking right now", body["detail"])
        self.assertEqual(self.job_status(jid), "active")
        self.sched["pause_job"].assert_not_called()
        self.request_cancel.assert_not_called()

    def test_pausing_a_booking_job_before_it_fires_is_done(self):
        jid = self.add_job(type_="book_next", status="active",
                           params={"date": future(), "time": "19:00", "duration": 120})
        status, body, _ = call("POST", f"/api/agent/v1/jobs/{jid}/pause")
        self.assertEqual((status, body["status"], body["pause"]), (200, "paused", "done"))

    def test_a_watch_that_stops_when_asked_is_paused(self):
        jid = self.add_job()
        rid = self.add_run(jid, "running")

        def thread_notices(job_id):  # what run_watch does at its next check
            with engine.begin() as conn:
                conn.execute(job_runs.update().where(job_runs.c.id == rid).values(status="cancelled"))
        self.request_cancel.side_effect = thread_notices
        status, body, _ = call("POST", f"/api/agent/v1/jobs/{jid}/pause")
        self.assertEqual((status, body["status"], body["pause"]), (200, "paused", "done"))

    def test_a_watch_mid_attempt_is_reported_as_stopping_not_paused(self):
        jid = self.add_job()
        self.add_run(jid, "running")
        with mock.patch.object(agent_api, "PAUSE_WAIT_S", 0.3):
            status, body, _ = call("POST", f"/api/agent/v1/jobs/{jid}/pause")
        self.assertEqual((status, body["pause"]), (200, "stopping"))
        # ...and it can't be resumed while that run is still on its way out.
        self.assertEqual(call("POST", f"/api/agent/v1/jobs/{jid}/resume")[0], 409)

    def test_a_watch_that_books_before_the_pause_lands_says_so(self):
        jid = self.add_job()
        rid = self.add_run(jid, "running")

        def it_booked(job_id):  # the run finished first and wrote its own outcome
            with engine.begin() as conn:
                conn.execute(job_runs.update().where(job_runs.c.id == rid).values(status="success"))
                conn.execute(jobs.update().where(jobs.c.id == jid).values(status="completed"))
        self.request_cancel.side_effect = it_booked
        status, body, _ = call("POST", f"/api/agent/v1/jobs/{jid}/pause")
        self.assertEqual((status, body["pause"], body["status"]), (200, "finished_first", "completed"))
        # The pause must not have written "paused" over the booked job.
        self.assertEqual(self.job_status(jid), "completed")

    def test_resuming_a_watch_replaces_its_trigger_rather_than_adding_one(self):
        # Found running the connector against the app: resume un-paused a watch's
        # unfired trigger AND scheduled another -- two runs for one job.
        jid = self.add_job(status="paused")
        with engine.begin() as conn:
            conn.execute(jobs.update().where(jobs.c.id == jid).values(apscheduler_id="watch_old"))
        with mock.patch("web.apscheduler_setup.remove_job") as remove:
            self.assertEqual(call("POST", f"/api/agent/v1/jobs/{jid}/resume")[0], 200)
        remove.assert_called_once_with("watch_old")
        self.sched["resume_job"].assert_not_called()
        self.sched["schedule_watch"].assert_called_once()

    def test_resuming_a_booking_job_unpauses_its_own_trigger(self):
        jid = self.add_job(type_="book_next", status="paused",
                           params={"date": future(), "time": "19:00", "duration": 120})
        self.assertEqual(call("POST", f"/api/agent/v1/jobs/{jid}/resume")[0], 200)
        self.sched["resume_job"].assert_called_once()
        self.sched["schedule_book_next"].assert_not_called()

    # 2. one spelling of a date.

    def test_compact_and_dashed_dates_are_the_same_day(self):
        d = future()
        first = self.created(call("POST", "/api/agent/v1/jobs/booking", self.booking(date=d))[1])
        self.assertEqual(first["date"], d)
        status, body, _ = call("POST", "/api/agent/v1/jobs/booking",
                               self.booking(date=d.replace("-", ""), times=["08:00"]))
        self.assertEqual(status, 409, body)
        self.assertIn(f"Job {first['id']}", body["detail"])

    def test_a_compact_date_is_stored_dashed(self):
        d = future()
        out = self.created(call("POST", "/api/agent/v1/jobs/watch",
                                self.booking(date=d.replace("-", "")))[1])
        self.assertEqual(out["date"], d)
        self.assertEqual(self.sched["schedule_watch"].call_args.args[2]["date"], d)

    # 3. no booking job and watch for the same day.

    def test_a_watch_for_a_day_a_booking_job_covers_is_refused(self):
        first = self.created(call("POST", "/api/agent/v1/jobs/booking", self.booking())[1])
        status, body, _ = call("POST", "/api/agent/v1/jobs/watch", self.booking(times=["08:00"]))
        self.assertEqual(status, 409)
        self.assertIn(f"Job {first['id']} (booking", body["detail"])

    def test_a_booking_job_for_a_day_a_watch_covers_is_refused(self):
        self.created(call("POST", "/api/agent/v1/jobs/watch", self.booking())[1])
        self.assertEqual(call("POST", "/api/agent/v1/jobs/booking", self.booking())[0], 409)

    def test_a_day_a_finished_job_already_booked_is_refused(self):
        d = future()
        jid = self.add_job(type_="book_next", status="completed",
                           params={"date": d, "time": "19:00", "duration": 120})
        rid = self.add_run(jid, "success")
        with engine.begin() as conn:
            conn.execute(insert(bookings).values(job_run_id=rid, account_id=self.acc_id, date=d,
                                                 start_time="19:00", court_type="Hard"))
        status, body, _ = call("POST", "/api/agent/v1/jobs/watch", self.booking(date=d))
        self.assertEqual(status, 409)
        self.assertIn(f"Job {jid} already booked", body["detail"])

    # 4. a failed reservation read must not delete anything.

    def _sync_with(self, reservations_or_error):
        """Run the real _do_sync with Playwright, login and the list read faked."""
        from web.routes import reservations as res

        browser = mock.MagicMock()
        pw = mock.MagicMock()
        pw.__enter__.return_value.chromium.launch.return_value = browser

        def read(page, org_cfg):
            # Only our account answers; anything else in the shared test DB raises, so
            # it is skipped rather than synced to nothing.
            if org_cfg.org_id != "99999":
                raise RuntimeError("not this test's account")
            if isinstance(reservations_or_error, Exception):
                raise reservations_or_error
            return reservations_or_error

        with mock.patch("playwright.sync_api.sync_playwright", return_value=pw), \
             mock.patch("auth.ensure_logged_in"), \
             mock.patch("booking.get_my_reservations", side_effect=read):
            return res._do_sync()

    def _seed_for_sync(self):
        """Point the account at the club (sync reads accounts.org_id) and give it one
        job-booked and one synced court."""
        self.set_account(self.acc_id, org_id=self.org_id)
        self.addCleanup(self.set_account, self.acc_id, org_id=None)
        jid = self.add_job(type_="book_next", status="completed",
                           params={"date": future(5), "time": "19:00"})
        rid = self.add_run(jid, "success")
        with engine.begin() as conn:
            conn.execute(insert(bookings).values(job_run_id=rid, account_id=self.acc_id,
                                                 date=future(5), start_time="19:00"))
            conn.execute(insert(bookings).values(job_run_id=None, account_id=self.acc_id,
                                                 date=future(6), start_time="08:00"))

    def _my_rows(self):
        with engine.connect() as conn:
            return sorted((r.date, r.start_time, r.job_run_id is not None) for r in conn.execute(
                bookings.select().where(bookings.c.account_id == self.acc_id)))

    def test_an_unreadable_list_deletes_nothing(self):
        from booking import ReservationListError
        self._seed_for_sync()
        before = self._my_rows()
        result = self._sync_with(ReservationListError("the reservation list didn't load"))
        self.assertEqual(self._my_rows(), before)
        self.assertEqual(result["removed"], 0)
        self.assertTrue(any("didn't load" in e for e in result["errors"]))

    def test_a_read_empty_list_never_deletes_a_job_booked_court(self):
        self._seed_for_sync()
        result = self._sync_with([])
        # The synced court is gone (CourtReserve says so); the job's court is kept.
        self.assertEqual(self._my_rows(), [(future(5), "19:00", True)])
        self.assertEqual((result["removed"], result["kept"]), (1, 1))

    def test_get_my_reservations_raises_rather_than_returning_nothing(self):
        from booking import OrgConfig, ReservationListError, get_my_reservations

        def page_answering(body):
            page = mock.MagicMock()
            handlers = []
            page.on.side_effect = lambda _ev, fn: handlers.append(fn)

            def goto(*a, **k):
                if body is not None:
                    resp = mock.MagicMock(url="https://x/my-bookings-portal/get-list?y")
                    resp.text.return_value = body
                    for fn in handlers:
                        fn(resp)
            page.goto.side_effect = goto
            return page

        org = OrgConfig(org_id="1", scheduler_id="1", cost_type_id="1")
        for body in (None, "<html>login</html>", "[]", '{"Errors": "x"}'):
            with self.assertRaises(ReservationListError, msg=body):
                get_my_reservations(page_answering(body), org)
        self.assertEqual(get_my_reservations(page_answering('{"Data": []}'), org), [])

    # 5. account must belong to the club; redactor; malformed hash.

    def test_an_account_not_linked_to_the_club_is_refused(self):
        status, body, _ = call("POST", "/api/agent/v1/jobs/booking",
                               self.booking(account_id=self.stray_acc_id))
        self.assertEqual(status, 409)
        self.assertIn("isn't linked", body["detail"])
        with mock.patch.object(agent_api, "_fetch_slots") as fetch:
            status, _, _ = call("GET", f"/api/agent/v1/availability?org_id={self.org_id}"
                                       f"&date={future()}&account_id={self.stray_acc_id}")
        self.assertEqual(status, 409)
        fetch.assert_not_called()

    def test_availability_never_falls_back_to_another_clubs_account(self):
        with mock.patch.object(agent_api, "_fetch_slots") as fetch:
            status, body, _ = call("GET", f"/api/agent/v1/availability?org_id={self.lonely_org_id}"
                                          f"&date={future()}")
        self.assertEqual(status, 409)
        self.assertIn("No account is linked to Other Club", body["detail"])
        fetch.assert_not_called()

    def test_the_redactor_catches_other_spellings(self):
        from urllib.parse import quote, quote_plus
        odd = 'Odd/&"pw 1'  # a password whose encodings differ from itself
        self.set_account(self.stray_acc_id, password=odd)
        self.addCleanup(self.set_account, self.stray_acc_id, password="StrayPw999")
        spellings = [PASSWORD.upper(), PASSWORD.lower(), quote(odd), quote(odd, safe=""),
                     quote_plus(odd), json.dumps(odd)[1:-1], EMAIL.upper(), quote(EMAIL)]
        jid = self.add_job()
        self.add_run(jid, "failed", "\n".join(f"line {i}: {v}" for i, v in enumerate(spellings)))
        _, body, raw = call("GET", f"/api/agent/v1/jobs/{jid}")
        for v in spellings:
            self.assertNotIn(v, body["runs"][0]["log_tail"], v)
        self.assertNotIn("Odd/", raw)
        self.assertNotIn(PASSWORD.lower(), raw.lower())
        self.assertEqual(body["runs"][0]["log_tail"].count("[redacted]"), 6)
        self.assertEqual(body["runs"][0]["log_tail"].count("p***@example.com"), 2)

    def test_a_malformed_hash_is_a_503_not_a_401(self):
        with mock.patch.dict(os.environ, {agent_api.TOKEN_HASH_ENV: TOKEN}):
            status, body, _ = call("GET", "/api/agent/v1/status")
        self.assertEqual(status, 503)
        self.assertIn("misconfigured", body["detail"])


class PauseRaceTests(AgentApiTestCase):
    """A run starting in the same moment as a pause (follow-up review of #4)."""

    def runs_of(self, job_id):
        with engine.connect() as conn:
            return [(r.status, r.log_text) for r in conn.execute(
                job_runs.select().where(job_runs.c.job_id == job_id))]

    def test_a_watch_that_starts_after_the_pause_exits_without_polling(self):
        # The pause wrote "paused" first; the run's trigger fired anyway.
        jid = self.add_job(status="paused")
        with mock.patch("web.job_runner.sync_playwright") as pw:
            jr.run_watch(jid, self.acc_id, future(), "19:00", 120)
        pw.assert_not_called()
        [(status, log)] = self.runs_of(jid)
        self.assertEqual(status, "cancelled")
        self.assertIn("Not started", log)
        self.assertEqual(self.job_status(jid), "paused")  # left to whoever paused it
        self.assertNotIn(jid, jr._cancel_events)

    def test_a_watch_registered_before_the_pause_is_cancelled_by_it(self):
        # The other order: the run registered and found the job active, then the pause
        # landed. Pause must still reach it, which needs "paused" written before the
        # cancel is sent and the real request_cancel.
        from web.routes.jobs import pause_job_by_id
        jid = self.add_job(status="active")
        event = jr._register_cancel(jid)
        self.addCleanup(jr._unregister_cancel, jid, event)
        self.assertTrue(jr._job_is_active(jid))
        seen = []
        self.request_cancel.side_effect = lambda job_id: (
            seen.append(self.job_status(job_id)), REAL_REQUEST_CANCEL(job_id))[1]
        pause_job_by_id(jid)
        self.assertTrue(event.is_set())
        self.assertEqual(seen, ["paused"])

    def test_a_scheduled_booking_run_on_a_paused_job_does_not_book(self):
        jid = self.add_job(type_="book_next", status="paused",
                           params={"date": future(), "time": "19:00", "duration": 120})
        with mock.patch("web.job_runner.sync_playwright") as pw:
            jr.run_book_next(jid, self.acc_id, target_date_iso=future(), target_time="19:00")
        pw.assert_not_called()
        self.assertEqual(self.runs_of(jid)[0][0], "cancelled")
        self.assertEqual(self.job_status(jid), "paused")

    def test_run_now_still_runs_a_job_that_is_not_active(self):
        # The UI's Run Now is the person asking, whatever the status: it gets through.
        jid = self.add_job(type_="book_next", status="paused",
                           params={"date": future(), "time": "19:00", "duration": 120})
        with mock.patch("web.job_runner.sync_playwright",
                        side_effect=RuntimeError("stop here")) as pw:
            jr.run_book_next(jid, self.acc_id, target_date_iso=future(), target_time="19:00",
                             manual=True)
        pw.assert_called_once()
        self.assertEqual(self.runs_of(jid)[0][0], "failed")  # it ran, and hit our stop

    def test_the_run_now_route_passes_manual(self):
        from web.routes import jobs as job_routes
        jid = self.add_job(type_="book_next", status="failed",
                           params={"date": future(), "time": "19:00", "duration": 120})
        with mock.patch("threading.Thread") as thread:
            asyncio.run(job_routes.run_now(jid))
        self.assertIs(thread.call_args.kwargs["target"], jr.run_book_next)
        self.assertTrue(thread.call_args.kwargs["kwargs"]["manual"])

    def test_a_paused_job_with_a_run_still_going_reads_as_stopping(self):
        jid = self.add_job(status="paused")
        self.add_run(jid, "running")
        _, j, _ = call("GET", f"/api/agent/v1/jobs/{jid}")
        self.assertEqual((j["status"], j["stopping"]), ("paused", True))
        listed = next(x for x in call("GET", "/api/agent/v1/jobs")[1]["jobs"] if x["id"] == jid)
        self.assertTrue(listed["stopping"])
        settled = self.add_job(status="paused")
        self.assertFalse(call("GET", f"/api/agent/v1/jobs/{settled}")[1]["stopping"])


class TransferTestCase(AgentApiTestCase):
    """Probe-to-main transfers. The fixture account is the probe; "Main" is a second
    account linked to the same club. CourtReserve is faked at job_runner's seams:
    Playwright, login, cancel_reservation and book_slot, and the result email."""

    MAIN_PASSWORD = "MainAcctPw#42"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with engine.begin() as conn:
            cls.main_id = conn.execute(insert(accounts).values(
                label="Main", email="main@example.com", password=cls.MAIN_PASSWORD,
                session_file="")).inserted_primary_key[0]
            cls.main_link = conn.execute(insert(account_orgs).values(
                account_id=cls.main_id, org_id=cls.org_id, is_resident=False)).inserted_primary_key[0]

    @classmethod
    def tearDownClass(cls):
        with engine.begin() as conn:
            conn.execute(account_orgs.delete().where(account_orgs.c.id == cls.main_link))
            conn.execute(accounts.delete().where(accounts.c.id == cls.main_id))
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.transfer_ids = []
        self.addCleanup(self._drop_transfers)
        self.fake = {}
        for name in ("sync_playwright", "ensure_logged_in", "cancel_reservation", "book_slot",
                     "_send_warning_email"):
            p = mock.patch(f"web.job_runner.{name}")
            self.fake[name] = p.start()
            self.addCleanup(p.stop)
        # No 0.5 s pauses between re-book tries.
        p = mock.patch("web.job_runner.time")
        p.start()
        self.addCleanup(p.stop)
        self.fake["cancel_reservation"].return_value = True
        self.fake["book_slot"].return_value = True

    def _drop_transfers(self):
        with engine.begin() as conn:
            conn.execute(pending_transfers.delete().where(
                pending_transfers.c.id.in_(self.transfer_ids)))

    def add_transfer(self, status="pending", reservation_id="12345678", date=None,
                     start_time="19:30", note="", probe=None, main=None):
        jid = self.add_job(params={"date": date or future(), "time": start_time})
        rid = self.add_run(jid, "pending_transfer")
        with engine.begin() as conn:
            tid = conn.execute(insert(pending_transfers).values(
                job_id=jid, run_id=rid, org_id=self.org_id,
                probe_account_id=probe or self.acc_id, main_account_id=main or self.main_id,
                reservation_id=reservation_id, date=date or future(), start_time=start_time,
                duration_min=30, court_type="Hard", status=status, auto=False, note=note,
                created_at=datetime.utcnow())).inserted_primary_key[0]
        self.transfer_ids.append(tid)
        return tid

    def transfer(self, tid):
        with engine.connect() as conn:
            return conn.execute(pending_transfers.select()
                                .where(pending_transfers.c.id == tid)).fetchone()

    def courts(self, tid):
        """(account, date, time) rows the app records for this transfer's run."""
        t = self.transfer(tid)
        with engine.connect() as conn:
            return sorted((r.account_id, r.date, r.start_time) for r in conn.execute(
                bookings.select().where(bookings.c.job_run_id == t.run_id)))

    def complete(self, tid, date=None, start_time="19:30", **extra):
        body = {"date": date or future(), "start_time": start_time, **extra}
        return call("POST", f"/api/agent/v1/transfers/{tid}/complete", body)


class TransferListTests(TransferTestCase):
    def test_open_transfers_are_listed_with_labels_only(self):
        tid = self.add_transfer(note=f"login as {EMAIL} with {PASSWORD} failed")
        done = self.add_transfer(status="transferred", start_time="20:00")
        status, body, raw = call("GET", "/api/agent/v1/transfers")
        self.assertEqual(status, 200)
        ids = [t["id"] for t in body["transfers"]]
        self.assertIn(tid, ids)
        self.assertNotIn(done, ids)  # open only, by default
        t = next(t for t in body["transfers"] if t["id"] == tid)
        self.assertEqual((t["status"], t["date"], t["start_time"]), ("pending", future(), "19:30"))
        self.assertEqual(t["main_account"], {"id": self.main_id, "label": "Main"})
        self.assertEqual(t["probe_account"]["label"], "p***@example.com")
        self.assertTrue(t["completable"])
        self.assertIsNone(t["why_not"])
        for secret in (PASSWORD, EMAIL, self.MAIN_PASSWORD, "main@example.com", "12345678"):
            self.assertNotIn(secret, raw)
        self.assertIn(done, [t["id"] for t in call("GET", "/api/agent/v1/transfers?state=all")[1]
                             ["transfers"]])
        self.assertEqual(call("GET", "/api/agent/v1/transfers?state=bogus")[0], 400)

    def test_why_a_transfer_cant_be_completed_is_listed(self):
        cases = {
            self.add_transfer(status="transferring"): "running right now",
            self.add_transfer(reservation_id=""): "no CourtReserve reservation id",
            self.add_transfer(date=(datetime.now(TZ).date() - timedelta(days=1)).isoformat()):
                "has passed",
            self.add_transfer(main=self.stray_acc_id): "isn't linked",
        }
        listed = {t["id"]: t for t in call("GET", "/api/agent/v1/transfers")[1]["transfers"]}
        for tid, why in cases.items():
            self.assertFalse(listed[tid]["completable"], why)
            self.assertIn(why, listed[tid]["why_not"])

    def test_listing_needs_the_token(self):
        self.assertEqual(call("GET", "/api/agent/v1/transfers", token=None)[0], 401)


class TransferCompleteTests(TransferTestCase):
    def test_a_transfer_moves_the_court_to_the_main_account(self):
        tid = self.add_transfer()
        status, body, raw = self.complete(tid)
        self.assertEqual(status, 200, body)
        self.assertEqual((body["outcome"], body["court"]), ("transferred", "main"))
        self.assertEqual(body["transfer"]["status"], "transferred")
        # The UI's code path: cancel as the probe, then book as main.
        self.assertEqual(self.fake["cancel_reservation"].call_args.args[1], "12345678")
        self.assertEqual(self.fake["book_slot"].call_count, 1)
        self.assertEqual(self.courts(tid), [(self.main_id, future(), "19:30")])
        self.assertNotIn(PASSWORD, raw)

    def test_a_second_call_is_refused_and_never_repeats_the_transfer(self):
        tid = self.add_transfer()
        self.assertEqual(self.complete(tid)[0], 200)
        status, body, _ = self.complete(tid)
        self.assertEqual(status, 409)
        self.assertIn("already moved", body["detail"])
        self.assertEqual(self.fake["cancel_reservation"].call_count, 1)

    def test_a_transfer_already_running_is_refused(self):
        tid = self.add_transfer(status="transferring")
        status, body, _ = self.complete(tid)
        self.assertEqual(status, 409)
        self.assertIn("running right now", body["detail"])
        self.fake["sync_playwright"].assert_not_called()
        self.assertEqual(self.transfer(tid).status, "transferring")

    def test_the_court_must_match(self):
        tid = self.add_transfer()
        for date, time_ in ((future(11), "19:30"), (future(), "20:00"), ("soon", "19:30")):
            status, body, _ = self.complete(tid, date=date, start_time=time_)
            self.assertEqual(status, 409, (date, time_))
            self.assertIn("Nothing was changed", body["detail"])
        self.fake["sync_playwright"].assert_not_called()
        self.assertEqual(self.transfer(tid).status, "pending")
        # The same day written compactly is the same court.
        self.assertEqual(self.complete(tid, date=future().replace("-", ""))[0], 200)

    def test_refusals_change_nothing(self):
        past = (datetime.now(TZ).date() - timedelta(days=1)).isoformat()
        for tid, date in ((self.add_transfer(status="failed"), None),
                          (self.add_transfer(reservation_id=""), None),
                          (self.add_transfer(date=past), past),
                          (self.add_transfer(probe=self.stray_acc_id), None)):
            before = self.transfer(tid).status
            status, body, _ = self.complete(tid, date=date)
            self.assertEqual(status, 409, body)
            self.assertEqual(self.transfer(tid).status, before)
        self.fake["sync_playwright"].assert_not_called()
        self.assertEqual(call("POST", "/api/agent/v1/transfers/999999/complete",
                              {"date": future(), "start_time": "19:30"})[0], 404)

    def test_only_the_court_is_accepted_in_the_body(self):
        tid = self.add_transfer()
        self.assertEqual(self.complete(tid, auto_transfer=True)[0], 422)
        self.assertEqual(call("POST", f"/api/agent/v1/transfers/{tid}/complete", {})[0], 422)
        self.assertEqual(call("POST", f"/api/agent/v1/transfers/{tid}/complete", None)[0], 422)
        self.fake["sync_playwright"].assert_not_called()

    def test_a_cancel_that_does_not_go_through_changes_nothing(self):
        tid = self.add_transfer()
        self.fake["cancel_reservation"].return_value = False
        body = self.complete(tid)[1]
        self.assertEqual((body["outcome"], body["court"]), ("failed_before_cancel", "probe"))
        self.assertIn("nothing changed", body["message"])
        self.fake["book_slot"].assert_not_called()
        self.assertEqual(self.transfer(tid).status, "held_by_probe")
        self.assertEqual(self.courts(tid), [(self.acc_id, future(), "19:30")])

    def test_a_failed_login_is_before_the_cancel(self):
        tid = self.add_transfer()
        self.fake["ensure_logged_in"].side_effect = RuntimeError(f"Login failed for {EMAIL}")
        status, body, raw = self.complete(tid)
        self.assertEqual((body["outcome"], body["court"]), ("failed_before_cancel", "probe"))
        self.assertIn("log in", body["message"])
        self.fake["cancel_reservation"].assert_not_called()
        self.assertNotIn(EMAIL, raw)
        # held by the probe and retryable
        self.assertTrue(body["transfer"]["completable"])

    @staticmethod
    def cancel_raising(after_submit):
        """A fake cancel that raises before, or after, it submits."""
        def cancel(page, rid, org, before_submit=None):
            if after_submit:
                before_submit()
            raise RuntimeError("Execution context was destroyed")
        return cancel

    def test_a_cancel_that_errors_after_submitting_is_stored_as_needing_a_check(self):
        tid = self.add_transfer()
        self.fake["cancel_reservation"].side_effect = self.cancel_raising(after_submit=True)
        body = self.complete(tid)[1]
        self.assertEqual((body["outcome"], body["court"]), ("cancel_uncertain", "unknown"))
        self.assertIn("MAY BE LOST", body["message"])
        self.fake["book_slot"].assert_not_called()
        # What's stored, listed and emailed says the same, and it isn't offered again.
        t = self.transfer(tid)
        self.assertEqual(t.status, "needs_check")
        self.assertEqual(t.note, jr.CANCEL_UNCERTAIN_NOTE)
        self.assertIsNotNone(t.cancel_submitted_at)
        self.assertIsNone(t.resolved_at)
        self.assertFalse(body["transfer"]["completable"])
        self.assertIn("MAY BE LOST", body["transfer"]["why_not"])
        subject, email = self.fake["_send_warning_email"].call_args.args
        self.assertIn("needs_check", subject)
        self.assertIn("may have gone through", email)
        self.assertNotIn("still holds", email)
        status, again, _ = self.complete(tid)
        self.assertEqual(status, 409)
        self.assertIn("A person must check CourtReserve", again["detail"])
        self.assertEqual(self.fake["cancel_reservation"].call_count, 1)
        # The UI's button can't run it either: the claim doesn't take needs_check.
        out = {}
        jr.run_transfer(tid, out)
        self.assertFalse(out["claimed"])
        self.assertEqual(self.courts(tid), [])  # nothing recorded as booked
        listed = {x["id"]: x for x in call("GET", "/api/agent/v1/transfers")[1]["transfers"]}
        self.assertEqual(listed[tid]["status"], "needs_check")

    def test_a_cancel_that_errors_before_submitting_changed_nothing(self):
        # Transfer #39, live: the reason field never appeared, so nothing was sent.
        tid = self.add_transfer()
        self.fake["cancel_reservation"].side_effect = self.cancel_raising(after_submit=False)
        body = self.complete(tid)[1]
        self.assertEqual((body["outcome"], body["court"]), ("failed_before_cancel", "probe"))
        self.assertIn("nothing was submitted", body["message"])
        t = self.transfer(tid)
        self.assertEqual(t.status, "held_by_probe")
        self.assertIn("before it was submitted", t.note)
        self.assertIsNone(t.cancel_submitted_at)
        self.assertTrue(body["transfer"]["completable"])

    def test_a_retry_clears_the_last_attempts_marker(self):
        tid = self.add_transfer(status="held_by_probe")
        with engine.begin() as conn:
            conn.execute(pending_transfers.update().where(pending_transfers.c.id == tid)
                         .values(cancel_submitted_at=datetime.utcnow()))
        self.fake["cancel_reservation"].return_value = False
        self.complete(tid)
        self.assertIsNone(self.transfer(tid).cancel_submitted_at)

    def test_a_court_the_probe_books_back_gets_its_new_id(self):
        tid = self.add_transfer()

        def book(page, url, slot, duration_minutes=60, org=None, result=None):
            if result is None:  # main's tries
                return False
            result["reservation_id"] = "87654321"  # the probe's new reservation
            return True

        self.fake["book_slot"].side_effect = book
        body = self.complete(tid)[1]
        self.assertEqual((body["outcome"], body["court"]), ("failed_after_cancel", "probe"))
        self.assertIn("holds it again", body["message"])
        t = self.transfer(tid)
        self.assertEqual((t.status, t.reservation_id), ("held_by_probe", "87654321"))
        self.assertEqual(self.fake["book_slot"].call_count, 4)  # 3 for main, 1 for the probe
        self.assertEqual(self.courts(tid), [(self.acc_id, future(), "19:30")])
        # A retry cancels the new reservation, never the one already cancelled.
        self.fake["book_slot"].side_effect = None
        self.assertEqual(self.complete(tid)[1]["outcome"], "transferred")
        self.assertEqual(self.fake["cancel_reservation"].call_args.args[1], "87654321")
        self.assertEqual(self.courts(tid), [(self.main_id, future(), "19:30")])

    def test_a_probe_book_back_without_an_id_cannot_be_retried(self):
        tid = self.add_transfer()
        self.fake["book_slot"].side_effect = lambda *a, result=None, **k: result is not None
        body = self.complete(tid)[1]
        self.assertEqual(body["outcome"], "failed_after_cancel")
        self.assertIn("can't be retried here", body["message"])
        self.assertEqual(self.transfer(tid).reservation_id, "")
        status, again, _ = self.complete(tid)
        self.assertEqual(status, 409)
        self.assertIn("no CourtReserve reservation id", again["detail"])
        self.assertEqual(self.fake["cancel_reservation"].call_count, 1)

    def test_a_lost_court_is_said_plainly_and_ends_the_transfer(self):
        tid = self.add_transfer(status="held_by_probe")
        self.fake["book_slot"].return_value = False
        body = self.complete(tid)[1]
        self.assertEqual((body["outcome"], body["court"]), ("failed_after_cancel", "lost"))
        self.assertIn("LOST", body["message"])
        self.assertEqual(self.transfer(tid).status, "failed")
        self.assertEqual(self.courts(tid), [])
        self.assertEqual(self.complete(tid)[0], 409)

    def test_a_browser_that_fails_to_close_keeps_the_result(self):
        tid = self.add_transfer()
        browser = self.fake["sync_playwright"].return_value.__enter__.return_value \
            .chromium.launch.return_value
        browser.close.side_effect = RuntimeError("browser has been closed")
        body = self.complete(tid)[1]
        self.assertEqual(body["outcome"], "transferred")
        self.assertEqual(self.transfer(tid).status, "transferred")

    def test_one_browser_at_a_time(self):
        tid = self.add_transfer()
        agent_api._browser_lock.acquire()
        try:
            status, body, _ = self.complete(tid)
        finally:
            agent_api._browser_lock.release()
        self.assertEqual(status, 409)
        self.assertIn("Nothing was changed", body["detail"])
        self.assertEqual(self.transfer(tid).status, "pending")

    def test_a_slow_transfer_answers_running_and_holds_the_browser(self):
        import threading
        tid = self.add_transfer()
        gate = threading.Event()
        self.fake["book_slot"].side_effect = lambda *a, **k: gate.wait(10)
        with mock.patch.object(agent_api, "TRANSFER_WAIT_S", 0.3):
            body = self.complete(tid)[1]
        self.assertEqual((body["outcome"], body["court"]), ("running", "unknown"))
        self.assertEqual(body["transfer"]["status"], "transferring")
        # Still running: a second complete and a browser check are both refused.
        self.assertIn("running right now", self.complete(tid)[1]["detail"])
        with mock.patch.object(agent_api, "_fetch_slots") as fetch:
            self.assertEqual(call("GET", f"/api/agent/v1/availability?org_id={self.org_id}"
                                         f"&date={future()}")[0], 409)
        fetch.assert_not_called()
        gate.set()
        for _ in range(100):
            if self.transfer(tid).status != "transferring" and not agent_api._browser_lock.locked():
                break
            time.sleep(0.05)
        self.assertEqual(self.transfer(tid).status, "transferred")
        self.assertFalse(agent_api._browser_lock.locked())

    def test_losing_the_claim_to_the_ui_is_a_409(self):
        # The UI's button claimed it between this call's checks and its own claim.
        tid = self.add_transfer()
        real = jr.run_transfer

        def ui_first(transfer_id, outcome=None):
            with engine.begin() as conn:
                conn.execute(pending_transfers.update().where(pending_transfers.c.id == transfer_id)
                             .values(status="transferring"))
            real(transfer_id, outcome)

        with mock.patch("web.job_runner.run_transfer", side_effect=ui_first):
            status, body, _ = self.complete(tid)
        self.assertEqual(status, 409)
        self.assertIn("started from elsewhere", body["detail"])
        self.fake["cancel_reservation"].assert_not_called()

    def test_the_ui_button_still_runs_the_same_code(self):
        from web.routes import jobs as job_routes
        tid = self.add_transfer()
        with mock.patch("threading.Thread") as thread:
            asyncio.run(job_routes.execute_transfer(tid))
        self.assertIs(thread.call_args.kwargs["target"], jr.run_transfer)
        thread.call_args.kwargs["target"](**thread.call_args.kwargs["kwargs"])
        self.assertEqual(self.transfer(tid).status, "transferred")


class TransferRecoveryTests(TransferTestCase):
    """A restart in the middle of a transfer (web/app.py _recover_stuck_transfers)."""

    def recover(self):
        from web.app import _recover_stuck_transfers
        _recover_stuck_transfers()

    def test_interrupted_before_the_cancel_is_safe_to_retry(self):
        tid = self.add_transfer(status="transferring")
        self.recover()
        t = self.transfer(tid)
        self.assertEqual(t.status, "held_by_probe")
        self.assertIn("before the cancel was sent", t.note)
        listed = {x["id"]: x for x in call("GET", "/api/agent/v1/transfers")[1]["transfers"]}
        self.assertTrue(listed[tid]["completable"])

    def test_interrupted_after_the_cancel_needs_a_check(self):
        tid = self.add_transfer(status="transferring")
        with engine.begin() as conn:
            conn.execute(pending_transfers.update().where(pending_transfers.c.id == tid)
                         .values(cancel_submitted_at=datetime.utcnow()))
        self.recover()
        t = self.transfer(tid)
        self.assertEqual(t.status, "needs_check")
        self.assertIn("may be on the probe, on the main account, or lost", t.note)
        status, body, _ = self.complete(tid)
        self.assertEqual(status, 409)
        self.assertIn("MAY BE LOST", body["detail"])
        self.fake["sync_playwright"].assert_not_called()

    def test_the_marker_is_written_when_the_cancel_is_submitted(self):
        # A real transfer that dies right after submitting is recovered as needs_check.
        tid = self.add_transfer()

        def cancel(page, rid, org, before_submit=None):
            before_submit()
            self.assertIsNotNone(self.transfer(tid).cancel_submitted_at)
            raise SystemExit("the process died here")  # not caught by the runner

        self.fake["cancel_reservation"].side_effect = cancel
        with self.assertRaises(SystemExit):
            jr.run_transfer(tid)
        self.assertEqual(self.transfer(tid).status, "transferring")
        self.recover()
        self.assertEqual(self.transfer(tid).status, "needs_check")

    def test_a_past_needs_check_transfer_is_closed(self):
        past = (datetime.now(TZ).date() - timedelta(days=1)).isoformat()
        tid = self.add_transfer(status="needs_check", date=past)
        self.recover()
        self.assertEqual(self.transfer(tid).status, "failed")


class TransferCheckedTests(TransferTestCase):
    """The UI's record of what a person found for a needs_check transfer."""

    def checked(self, tid, holder):
        from web.routes import jobs as job_routes
        return asyncio.run(job_routes.transfer_checked(tid, holder))

    def test_the_probe_still_holding_it_makes_it_completable_again(self):
        tid = self.add_transfer(status="needs_check")
        self.checked(tid, "probe")
        t = self.transfer(tid)
        self.assertEqual(t.status, "held_by_probe")
        self.assertIn("Checked on CourtReserve by a person", t.note)
        self.assertEqual(self.courts(tid), [(self.acc_id, future(), "19:30")])
        self.assertEqual(self.complete(tid)[1]["outcome"], "transferred")

    def test_main_having_it_or_closing_it_ends_the_transfer(self):
        for holder, status, courts in (("main", "transferred", [(None, future(), "19:30")]),
                                       ("closed", "failed", [])):
            tid = self.add_transfer(status="needs_check")
            self.checked(tid, holder)
            self.assertEqual(self.transfer(tid).status, status, holder)
            expected = [(self.main_id, d, t) for _, d, t in courts]
            self.assertEqual(self.courts(tid), expected, holder)
            self.assertEqual(self.complete(tid)[0], 409, holder)

    def test_only_a_needs_check_transfer_is_changed(self):
        tid = self.add_transfer(status="pending")
        self.checked(tid, "main")
        self.assertEqual(self.transfer(tid).status, "pending")
        self.assertEqual(self.checked(tid, "bogus").status_code, 400)

    def test_the_agent_cannot_record_a_check(self):
        tid = self.add_transfer(status="needs_check")
        for path in (f"/transfers/{tid}/checked", f"/transfers/{tid}/confirm"):
            self.assertIn(call("POST", "/api/agent/v1" + path, {"holder": "probe"})[0], (404, 405))
        self.assertEqual(self.transfer(tid).status, "needs_check")


class AutoTransferTests(TransferTestCase):
    def test_an_auto_transfer_is_never_claimable_while_its_notice_sends(self):
        jid = self.add_job()
        rid = self.add_run(jid)
        seen = []

        def email(subject, body):
            with engine.connect() as conn:
                row = conn.execute(pending_transfers.select()
                                   .where(pending_transfers.c.run_id == rid)).fetchone()
            self.transfer_ids.append(row.id)
            seen.append(row.status)
            return "sent"

        self.fake["_send_warning_email"].side_effect = email
        with engine.connect() as conn:
            org = row_to_dict(conn.execute(organizations.select()
                                           .where(organizations.c.id == self.org_id)).fetchone())
            probe = row_to_dict(conn.execute(accounts.select()
                                             .where(accounts.c.id == self.acc_id)).fetchone())
            main = row_to_dict(conn.execute(accounts.select()
                                            .where(accounts.c.id == self.main_id)).fetchone())
        slot = jr._slot_from_fields(future(), "19:30", 30, "Hard", "America/Los_Angeles")
        status = jr._handle_probe_secured(jid, rid, org, jr._org_config(org), main, probe,
                                          mock.MagicMock(), mock.MagicMock(), slot, 30, True, TZ,
                                          captured_reservation_id="12345678")
        self.assertEqual(status, "success")
        self.assertEqual(seen[0], "transferring")  # the "court secured" notice
        listed = {t["id"]: t for t in call("GET", "/api/agent/v1/transfers?state=all")[1]
                  ["transfers"]}
        self.assertEqual(listed[self.transfer_ids[-1]]["status"], "transferred")

    def test_an_auto_transfer_whose_cancel_may_have_gone_through_needs_a_check(self):
        jid = self.add_job()
        rid = self.add_run(jid)
        self.fake["cancel_reservation"].side_effect = TransferCompleteTests.cancel_raising(True)
        with engine.connect() as conn:
            org = row_to_dict(conn.execute(organizations.select()
                                           .where(organizations.c.id == self.org_id)).fetchone())
            probe = row_to_dict(conn.execute(accounts.select()
                                             .where(accounts.c.id == self.acc_id)).fetchone())
            main = row_to_dict(conn.execute(accounts.select()
                                            .where(accounts.c.id == self.main_id)).fetchone())
        slot = jr._slot_from_fields(future(), "19:30", 30, "Hard", "America/Los_Angeles")
        status = jr._handle_probe_secured(jid, rid, org, jr._org_config(org), main, probe,
                                          mock.MagicMock(), mock.MagicMock(), slot, 30, True, TZ,
                                          captured_reservation_id="12345678")
        with engine.connect() as conn:
            row = conn.execute(pending_transfers.select()
                               .where(pending_transfers.c.run_id == rid)).fetchone()
            booked = conn.execute(bookings.select().where(bookings.c.job_run_id == rid)).fetchall()
        self.transfer_ids.append(row.id)
        self.assertEqual(status, "pending_transfer")
        self.assertEqual((row.status, row.note), ("needs_check", jr.CANCEL_UNCERTAIN_NOTE))
        self.assertIsNotNone(row.cancel_submitted_at)
        self.assertEqual(booked, [])
        self.assertIn("may have gone through", self.fake["_send_warning_email"].call_args.args[1])


class MintTokenTests(unittest.TestCase):
    def test_the_env_line_is_the_tokens_hash(self):
        from scripts.mint_agent_token import mint
        token, digest = mint()
        self.assertTrue(token.startswith("cr_agent_"))
        self.assertEqual(digest, hashlib.sha256(token.encode()).hexdigest())
        self.assertNotEqual(mint()[0], token)


if __name__ == "__main__":
    unittest.main()
