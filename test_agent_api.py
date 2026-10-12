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
import unittest
from datetime import datetime, timedelta
from unittest import mock

import pytz

# Must be set before importing web.database (reads DATA_DIR at import).
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cr-test-data-"))

from fastapi import FastAPI
from sqlalchemy import insert

from web.database import (account_orgs, accounts, bookings, engine, init_db, job_runs, jobs,
                          organizations)
from web.routes import agent_api

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

    @classmethod
    def tearDownClass(cls):
        with engine.begin() as conn:
            conn.execute(account_orgs.delete().where(account_orgs.c.id == cls.link_id))
            conn.execute(accounts.delete().where(accounts.c.id == cls.acc_id))
            conn.execute(organizations.delete().where(organizations.c.id == cls.org_id))

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


class MintTokenTests(unittest.TestCase):
    def test_the_env_line_is_the_tokens_hash(self):
        from scripts.mint_agent_token import mint
        token, digest = mint()
        self.assertTrue(token.startswith("cr_agent_"))
        self.assertEqual(digest, hashlib.sha256(token.encode()).hexdigest())
        self.assertNotEqual(mint()[0], token)


if __name__ == "__main__":
    unittest.main()
