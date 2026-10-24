"""A narrow JSON API for the home agent (hs220/home-agent, connector `courtreserve`).

The agent reaches this over the `court-reserve-share` Docker network as
`court-reserve-web:8080`; nothing else of home-agent's can. It gets exactly what its
tools need and nothing more:

- read: jobs and their runs, upcoming reservations the app knows about, clubs and
  account labels, and a live availability check for one date;
- write: create a one-shot booking or watch job, pause or resume a job, re-sync
  the reservations mirror from CourtReserve, and complete a pending probe-to-main
  transfer (the UI's Transfer button, through the same code).

What is deliberately absent: deleting, editing, restarting or run-now on a job;
cancelling a reservation; creating a job with a probe account or auto-transfer;
anything about accounts or organizations beyond their names. A missing endpoint is the only guarantee that survives a
confused or misled agent.

**No secrets leave.** Responses never carry a CourtReserve password, an email
address or a session file. Free text that originates in a run (logs, errors) is
passed through `_redactor`, which replaces every stored account password and the
app's own secrets from the environment, and swaps account emails for their labels:
a Playwright error or a future log line must not be the way one leaks.

**Auth.** A bearer token whose SHA-256 is `AGENT_API_TOKEN_SHA256` in `.env`
(scripts/mint_agent_token.py makes one). The plaintext lives only in home-agent's
broker. Unset means the API is off: every call answers 503.
"""

import hashlib
import hmac
import json
import os
import re
import threading
import time
from datetime import date as date_cls, datetime, timedelta, timezone
from urllib.parse import quote, quote_plus

import pytz
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text
from starlette.concurrency import run_in_threadpool

from web import apscheduler_setup
from web.database import (account_orgs, accounts, engine, job_runs, jobs, organizations,
                          pending_transfers, row_to_dict)

TOKEN_HASH_ENV = "AGENT_API_TOKEN_SHA256"
#: The app's own secrets, scrubbed from any text the API returns.
ENV_SECRETS = ("CR_PASSWORD", "SMTP_PASSWORD", "GITHUB_TOKEN")

KINDS = {"book_next": "booking", "watch": "watch",
         "recurrent_book_next": "recurring_booking", "recurrent_watch": "recurring_watch"}
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DURATIONS = (30, 60, 90, 120)
#: The web form's "stop watching" choices. "infinite" is left out on purpose: a watch
#: that can book inside the late-cancellation window should be the person's own choice.
STOP_BEFORE = ("5h10m", "4h10m", "30m")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):(00|30)$")
MAX_TIMES = 6
MAX_DAYS_AHEAD = 60
LOG_TAIL_LINES = 20
LOG_TAIL_CHARS = 2000
WATCH_INTERVAL_S = 60
#: How long pause waits for a watch's in-flight run to notice and stop. A watch checks
#: between polls, so this is normally well under a second; it is longer only when the
#: run is mid-attempt at booking, which can't be interrupted.
PAUSE_WAIT_S = 20.0
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
#: How long completing a transfer waits for its result before answering "running". A
#: transfer logs in twice, cancels, then tries the main account up to three times:
#: typically 30-100 s. The connector's timeout must stay above this.
TRANSFER_WAIT_S = 150.0
OPEN_TRANSFERS = ("pending", "held_by_probe", "transferring", "needs_check")

#: One browser at a time for agent-initiated work. Each availability check or sync
#: launches Chromium and may log in; a watcher asking in a loop must not be able to
#: stack those up on the NAS next to the watch jobs that matter.
_browser_lock = threading.Lock()


def _require_token(authorization: str = Header(default="")) -> None:
    want = os.environ.get(TOKEN_HASH_ENV, "").strip().lower()
    if not want:
        raise HTTPException(503, f"The agent API is off: {TOKEN_HASH_ENV} is not set.")
    # A value that can't be a SHA-256 (the plaintext token pasted by mistake, a
    # truncated line) would refuse every token as 401, which reads as "the agent's token
    # is wrong" when it's the app that's misconfigured.
    if not SHA256_HEX.match(want):
        raise HTTPException(503, f"The agent API is misconfigured: {TOKEN_HASH_ENV} is not a "
                                 "SHA-256 hex digest.")
    scheme, _, token = authorization.partition(" ")
    got = hashlib.sha256(token.strip().encode()).hexdigest()
    # compare_digest on the hashes, so the check takes the same time however much of
    # a guess is right.
    if scheme.lower() != "bearer" or not token.strip() or not hmac.compare_digest(got, want):
        raise HTTPException(401, "Bad or missing token.", headers={"WWW-Authenticate": "Bearer"})


router = APIRouter(prefix="/api/agent/v1", dependencies=[Depends(_require_token)])


# ── helpers ──────────────────────────────────────────────────────────────────

def _mask_email(email: str) -> str:
    user, _, domain = (email or "").partition("@")
    return f"{user[:1]}***@{domain}" if domain else "account"


def _label(acc) -> str:
    acc = acc if isinstance(acc, dict) else row_to_dict(acc)
    return acc.get("label") or _mask_email(acc.get("email", ""))


def _encodings(value: str) -> set[str]:
    """The forms a value takes in the text a run produces: as typed, in a URL or form
    body, and inside a JSON string (a logged request or response)."""
    return {value, quote(value), quote(value, safe=""), quote_plus(value),
            json.dumps(value)[1:-1]}


def _redactor(conn):
    """A function that makes run text safe to return: secrets out, emails to labels.
    Case-insensitive, and over each value's encoded forms too: what matters is that no
    spelling of a password gets out, and over-redacting a look-alike costs nothing."""
    rows = conn.execute(select(accounts)).fetchall()
    secrets = {r.password for r in rows if r.password}
    secrets |= {os.environ[k] for k in ENV_SECRETS if os.environ.get(k)}
    forms = set().union(*(_encodings(v) for v in secrets)) if secrets else set()
    # Longest first, so a password containing another is replaced whole.
    secret_re = (re.compile("|".join(map(re.escape, sorted(forms, key=len, reverse=True))),
                            re.IGNORECASE) if forms else None)
    emails = [(re.compile("|".join(map(re.escape, sorted(_encodings(r.email), key=len,
                                                          reverse=True))), re.IGNORECASE),
               _label(r)) for r in rows if r.email]

    def scrub(s: str) -> str:
        if secret_re:
            s = secret_re.sub("[redacted]", s)
        for pattern, label in emails:
            s = pattern.sub(lambda _m, label=label: label, s)
        return s

    return scrub


def _iso_utc(dt) -> str | None:
    """The app stores utcnow() naive; say so on the way out."""
    if not dt:
        return None
    if isinstance(dt, str):
        return dt
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).isoformat()


def _times(val) -> list[str]:
    if isinstance(val, list):
        return [str(t).strip() for t in val if str(t).strip()]
    return [t.strip() for t in str(val or "").split(",") if t.strip()]


def _org(conn, org_id: int) -> dict:
    row = conn.execute(organizations.select().where(organizations.c.id == org_id)).fetchone()
    if not row:
        raise HTTPException(404, f"No club with id {org_id}.")
    return row_to_dict(row)


def _job_row(conn, job_id: int) -> dict:
    row = conn.execute(jobs.select().where(jobs.c.id == job_id)).fetchone()
    if not row:
        raise HTTPException(404, f"No job with id {job_id}.")
    return row_to_dict(row)


def _job_out(conn, j: dict) -> dict:
    params = json.loads(j.get("params") or "{}")
    org = conn.execute(organizations.select().where(organizations.c.id == j["org_id"])).fetchone()
    acc = conn.execute(accounts.select().where(accounts.c.id == j["account_id"])).fetchone()
    out = {
        "id": j["id"],
        "kind": KINDS.get(j["type"], j["type"]),
        "status": j["status"],
        "club": {"id": j["org_id"], "name": org.name if org else "?"},
        "account": {"id": j["account_id"], "label": _label(acc) if acc else "?"},
        "times": _times(params.get("time")),
        "duration_min": int(params.get("duration") or 0) or None,
        "created_at": _iso_utc(j.get("created_at")),
    }
    if j["type"].startswith("recurrent_"):
        dow = params.get("target_day_of_week")
        out["day_of_week"] = DAY_NAMES[int(dow)] if dow is not None else None
    else:
        out["date"] = params.get("date")
        # Local time in the club's timezone, 2 min before the booking window opens.
        out["starts_at"] = params.get("run_at") or None
    if j["type"].endswith("watch"):
        out["stop_before_court"] = params.get("deadline_mode")
    if params.get("probe_account_id"):
        out["probe_account_id"] = params["probe_account_id"]
    next_run = apscheduler_setup.get_next_run(j.get("apscheduler_id") or "")
    out["next_fire"] = next_run.isoformat() if next_run else None

    last = conn.execute(
        job_runs.select().where(job_runs.c.job_id == j["id"]).order_by(job_runs.c.id.desc()).limit(1)
    ).fetchone()
    # Paused, but a run hasn't finished stopping: it may still book. Said here, not only
    # in pause's reply, so list and get never show a plain "paused" in that window.
    out["stopping"] = bool(j["status"] == "paused" and last and last.status == "running")
    out["last_run"] = ({"id": last.id, "status": last.status,
                        "started_at": _iso_utc(last.started_at),
                        "finished_at": _iso_utc(last.finished_at)} if last else None)
    out["booked"] = [dict(r._mapping) for r in conn.execute(text("""
        SELECT b.date, b.start_time, b.duration_min, b.court_type
        FROM bookings b JOIN job_runs jr ON jr.id = b.job_run_id
        WHERE jr.job_id = :jid ORDER BY b.date, b.start_time
    """), {"jid": j["id"]})]
    return out


# ── read ─────────────────────────────────────────────────────────────────────

@router.get("/status")
def status():
    """The setup test: answers without touching CourtReserve."""
    today = datetime.now(pytz.timezone("America/Los_Angeles")).date().isoformat()
    with engine.connect() as conn:
        counts = dict(conn.execute(text(
            "SELECT status, count(*) FROM jobs GROUP BY status")).fetchall())
        upcoming = conn.execute(text(
            "SELECT count(*) FROM bookings WHERE date >= :d"), {"d": today}).scalar()
        clubs = conn.execute(text("SELECT count(*) FROM organizations")).scalar()
        n_accounts = conn.execute(text("SELECT count(*) FROM accounts")).scalar()
    return {"app": "court-reserve", "jobs": counts, "upcoming_reservations": upcoming,
            "clubs": clubs, "accounts": n_accounts}


@router.get("/clubs")
def clubs():
    """Clubs with their booking-window rules, and the accounts by label only."""
    with engine.connect() as conn:
        orgs = [row_to_dict(r) for r in conn.execute(organizations.select().order_by(organizations.c.id))]
        accs = {r.id: r for r in conn.execute(accounts.select().order_by(accounts.c.id))}
        links = conn.execute(account_orgs.select()).fetchall()
    out = []
    for o in orgs:
        linked = []
        for link in links:
            if link.org_id == o["id"] and link.account_id in accs:
                resident = bool(link.is_resident)
                linked.append({
                    "id": link.account_id,
                    "label": _label(accs[link.account_id]),
                    "resident": resident,
                    "days_out": o["resident_days_out"] if resident else o["nonresident_days_out"],
                })
        out.append({
            "id": o["id"], "name": o["name"], "timezone": o["timezone"],
            "release_time": f"{o.get('release_hour', 12):02d}:{o.get('release_minute', 0):02d}",
            "days_out_default": o["nonresident_days_out"],
            "accounts": linked,
        })
    return {"clubs": out,
            "accounts": [{"id": a.id, "label": _label(a)} for a in accs.values()]}


@router.get("/jobs")
def list_jobs(state: str = "current", limit: int = 20):
    if state not in ("current", "finished", "all"):
        raise HTTPException(400, "state is current, finished or all.")
    limit = max(1, min(limit, 100))
    q = jobs.select().order_by(jobs.c.created_at.desc(), jobs.c.id.desc()).limit(limit)
    if state == "current":
        q = q.where(jobs.c.status.in_(["active", "paused"]))
    elif state == "finished":
        q = q.where(jobs.c.status.notin_(["active", "paused"]))
    with engine.connect() as conn:
        return {"jobs": [_job_out(conn, row_to_dict(r)) for r in conn.execute(q)]}


@router.get("/jobs/{job_id}")
def get_job(job_id: int, runs: int = 5):
    runs = max(1, min(runs, 20))
    with engine.connect() as conn:
        out = _job_out(conn, _job_row(conn, job_id))
        scrub = _redactor(conn)
        rows = conn.execute(
            job_runs.select().where(job_runs.c.job_id == job_id)
            .order_by(job_runs.c.id.desc()).limit(runs)
        ).fetchall()
    out["runs"] = []
    for r in rows:
        tail = "\n".join((r.log_text or "").strip().splitlines()[-LOG_TAIL_LINES:])
        out["runs"].append({
            "id": r.id, "status": r.status,
            "started_at": _iso_utc(r.started_at), "finished_at": _iso_utc(r.finished_at),
            "log_tail": scrub(tail)[-LOG_TAIL_CHARS:],
        })
    return out


@router.get("/reservations")
def reservations(days: int = 30):
    """Upcoming courts the app knows about: booked by its jobs, or found by a sync."""
    days = max(1, min(days, 90))
    today = datetime.now(pytz.timezone("America/Los_Angeles")).date()
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT b.date, b.start_time, b.duration_min, b.court_type, b.account_id,
                   jr.job_id AS job_id,
                   COALESCE(jo.name, ao.name) AS club
            FROM bookings b
            LEFT JOIN job_runs jr ON jr.id = b.job_run_id
            LEFT JOIN jobs j ON j.id = jr.job_id
            LEFT JOIN organizations jo ON jo.id = j.org_id
            LEFT JOIN accounts a ON a.id = b.account_id
            LEFT JOIN organizations ao ON ao.id = a.org_id
            WHERE b.date >= :start AND b.date <= :end
            ORDER BY b.date, b.start_time
        """), {"start": today.isoformat(),
               "end": (today + timedelta(days=days)).isoformat()}).fetchall()
        labels = {a.id: _label(a) for a in conn.execute(accounts.select())}
    return {"reservations": [{
        "date": r.date, "start_time": r.start_time, "duration_min": r.duration_min,
        "court_type": r.court_type, "club": r.club,
        "account": labels.get(r.account_id, "?"),
        "source": f"job {r.job_id}" if r.job_id else "synced from CourtReserve",
    } for r in rows]}


def _fetch_slots(org: dict, account: dict, target_date: date_cls) -> list[dict]:
    """Log in (reusing the jobs' saved session for this account and club) and read the
    slot feed for one date. A read: it POSTs to CourtReserve's ReadConsolidated feed,
    which returns the grid and books nothing."""
    from playwright.sync_api import sync_playwright
    from auth import BROWSER_ARGS, USER_AGENT, ensure_logged_in
    from booking import get_available_slots
    from web.job_runner import _org_config, _session_file

    org_cfg = _org_config(org)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=BROWSER_ARGS)
        try:
            ctx = browser.new_context(user_agent=USER_AGENT)
            page = ctx.new_page()
            ensure_logged_in(ctx, page, account["email"], account["password"],
                             org_cfg.booking_url, _session_file(account, org))
            slots = get_available_slots(page, target_date, org_cfg)
        finally:
            browser.close()
    return [{"start_time": s.start_time, "court_type": s.court_type,
             "available_courts": s.available_courts, "waitlist": s.is_wait_list}
            for s in sorted(slots, key=lambda s: (s.start_ms, s.court_type))]


def _club_account(conn, org: dict, account_id: int | None) -> dict:
    """The account to act as at this club: the one given, or the club's first linked
    one. Either way it must be linked to the club (Clubs page), so one club's
    credentials are never tried on another club's site."""
    linked = select(accounts).join(account_orgs, account_orgs.c.account_id == accounts.c.id) \
        .where(account_orgs.c.org_id == org["id"])
    if account_id is not None:
        if not conn.execute(accounts.select().where(accounts.c.id == account_id)).fetchone():
            raise HTTPException(404, f"No account with id {account_id}.")
        row = conn.execute(linked.where(accounts.c.id == account_id)).fetchone()
        if not row:
            raise HTTPException(409, f"Account {account_id} isn't linked to {org['name']}. "
                                     "Link it on the app's Clubs page first.")
        return row_to_dict(row)
    row = conn.execute(linked.order_by(accounts.c.id).limit(1)).fetchone()
    if not row:
        raise HTTPException(409, f"No account is linked to {org['name']}. "
                                 "Link one on the app's Clubs page first.")
    return row_to_dict(row)


def _iso(value) -> str | None:
    """A date as YYYY-MM-DD however it was written (3.12's fromisoformat also takes
    20261022 and week dates), so stored and compared dates are one spelling."""
    try:
        return date_cls.fromisoformat(str(value)).isoformat()
    except ValueError:
        return None


@router.get("/availability")
async def availability(org_id: int, date: str, account_id: int | None = None):
    try:
        target = date_cls.fromisoformat(date)
    except ValueError:
        raise HTTPException(400, "date is YYYY-MM-DD.")
    with engine.connect() as conn:
        org = _org(conn, org_id)
        account = _club_account(conn, org, account_id)
        scrub = _redactor(conn)
    if not _browser_lock.acquire(blocking=False):
        raise HTTPException(409, "Another availability check or sync is running; try again in a minute.")
    try:
        slots = await run_in_threadpool(_fetch_slots, org, account, target)
    except RuntimeError as e:
        if "Login failed" in str(e):
            raise HTTPException(502, f"CourtReserve refused the login for {_label(account)}.")
        raise HTTPException(502, f"CourtReserve check failed: {scrub(str(e))[:300]}")
    except Exception as e:
        raise HTTPException(502, f"CourtReserve check failed: {scrub(str(e))[:300]}")
    finally:
        _browser_lock.release()
    return {"club": {"id": org["id"], "name": org["name"]}, "date": target.isoformat(),
            "checked_as": _label(account), "slots": slots}


# ── write ────────────────────────────────────────────────────────────────────

class NewBooking(BaseModel):
    model_config = ConfigDict(extra="forbid")
    org_id: int
    account_id: int
    date: str
    times: list[str]
    duration_min: int = 120


class NewWatch(NewBooking):
    stop_before: str = "4h10m"


#: The job types that book one court on one date. A booking job and a watch for the
#: same day are as much a double booking as two of either.
ONE_SHOT = ("book_next", "watch")


def _validate(conn, body: NewBooking) -> tuple[dict, str, list[str]]:
    """Checks a new job and returns the club, the date as YYYY-MM-DD, and the times."""
    org = _org(conn, body.org_id)
    _club_account(conn, org, body.account_id)
    tz = pytz.timezone(org["timezone"])
    now = datetime.now(tz)
    iso = _iso(body.date)
    if iso is None:
        raise HTTPException(400, "date is YYYY-MM-DD.")
    d = date_cls.fromisoformat(iso)
    if not now.date() <= d <= now.date() + timedelta(days=MAX_DAYS_AHEAD):
        raise HTTPException(400, f"date must be between today and {MAX_DAYS_AHEAD} days ahead.")
    times = list(dict.fromkeys(t.strip() for t in body.times))
    # At least one time, always: with none, the job runner books whatever it finds.
    if not times or len(times) > MAX_TIMES or not all(TIME_RE.match(t) for t in times):
        raise HTTPException(400, f"times is 1-{MAX_TIMES} start times like 19:00 or 19:30, "
                                 "in order of preference.")
    past = [t for t in times
            if tz.localize(datetime.combine(d, datetime.strptime(t, "%H:%M").time())) <= now]
    if past:
        raise HTTPException(400, f"Already past: {', '.join(past)}.")
    if body.duration_min not in DURATIONS:
        raise HTTPException(400, f"duration_min is one of {', '.join(map(str, DURATIONS))}.")
    # Two jobs for the same court day would book twice: say which one exists instead.
    # Booking and watch jobs alike, and stored dates normalised too, so neither the
    # job type nor the spelling of the date is a way past this.
    for r in conn.execute(jobs.select().where(
            jobs.c.type.in_(ONE_SHOT) & (jobs.c.account_id == body.account_id) &
            (jobs.c.org_id == body.org_id) & jobs.c.status.in_(["active", "paused"]))):
        if _iso(json.loads(r.params or "{}").get("date")) == iso:
            raise HTTPException(409, f"Job {r.id} ({KINDS[r.type]}, {r.status}) already covers "
                                     f"{iso} for this account and club.")
    # ...and a court one of its jobs already booked that day is the same mistake, later.
    done = conn.execute(text("""
        SELECT j.id FROM bookings b
        JOIN job_runs jr ON jr.id = b.job_run_id JOIN jobs j ON j.id = jr.job_id
        WHERE b.account_id = :aid AND j.org_id = :oid AND b.date = :d LIMIT 1
    """), {"aid": body.account_id, "oid": body.org_id, "d": iso}).fetchone()
    if done:
        raise HTTPException(409, f"Job {done.id} already booked a court on {iso} for this "
                                 "account and club.")
    return org, iso, times


def _created(job_id: int, org: dict) -> dict:
    with engine.connect() as conn:
        out = _job_out(conn, _job_row(conn, job_id))
    out["fires"] = (f"at {out['starts_at']} {org['timezone']}, 2 min before the booking window opens"
                    if out.get("starts_at") else "now: the booking window is already open")
    return out


@router.post("/jobs/booking")
def create_booking(body: NewBooking):
    """A one-shot booking job: at the window's opening it books the first of `times`
    that is free. Booking commits the person to the court."""
    from web.routes.jobs import create_book_next_job
    with engine.connect() as conn:
        org, iso, times = _validate(conn, body)
    job_id = create_book_next_job(body.account_id, body.org_id, iso, ",".join(times),
                                  body.duration_min)
    return _created(job_id, org)


@router.post("/jobs/watch")
def create_watch(body: NewWatch):
    """A watch job: polls for a cancellation at one of `times` and books it when one opens."""
    from web.routes.jobs import create_watch_job
    if body.stop_before not in STOP_BEFORE:
        raise HTTPException(400, f"stop_before is one of {', '.join(STOP_BEFORE)}.")
    with engine.connect() as conn:
        org, iso, times = _validate(conn, body)
    job_id = create_watch_job(body.account_id, body.org_id, iso, ",".join(times),
                              body.duration_min, WATCH_INTERVAL_S, body.stop_before)
    return _created(job_id, org)


def _running_run(conn, job_id: int):
    return conn.execute(
        job_runs.select().where((job_runs.c.job_id == job_id) & (job_runs.c.status == "running"))
        .order_by(job_runs.c.id.desc()).limit(1)
    ).fetchone()


@router.post("/jobs/{job_id}/pause")
def pause(job_id: int):
    """Pause a job, and say what really happened.

    A booking job's run (from 2 min before the window opens until it has booked or
    given up) does not listen for cancellation, so pausing it then would stop nothing
    while claiming to: refused with 409, nothing changed. A watch stops at its next
    check between polls; this waits for that, and reports `pause` as `done`, as
    `stopping` when the run is mid-attempt and may still book, or as `finished_first`
    when the job ended on its own (booked, or failed) before the pause reached it."""
    from web.routes.jobs import pause_job_by_id
    with engine.connect() as conn:
        j = _job_row(conn, job_id)
        running = _running_run(conn, job_id)
    if j["status"] != "active":
        raise HTTPException(409, f"Job {job_id} is {j['status']}; only an active job can be paused.")
    if j["type"] == "book_next" and running:
        raise HTTPException(409, f"Job {job_id} is booking right now (run {running.id}) and can't "
                                 "be stopped; it finishes within minutes. Nothing was changed.")
    pause_job_by_id(job_id)
    deadline = time.monotonic() + PAUSE_WAIT_S
    while True:
        with engine.connect() as conn:
            still = _running_run(conn, job_id)
            out = _job_out(conn, _job_row(conn, job_id))
        if not still or time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    if out["status"] != "paused":
        out["pause"] = "finished_first"
    elif out["stopping"]:
        # Covers a booking job whose trigger fired between the check above and the
        # pause, too: it is running and can't be stopped.
        out["pause"] = "stopping"
    else:
        out["pause"] = "done"
    return out


@router.post("/jobs/{job_id}/resume")
def resume(job_id: int):
    from web.routes.jobs import resume_job_by_id
    with engine.connect() as conn:
        j = _job_row(conn, job_id)
        org = _org(conn, j["org_id"])
        running = _running_run(conn, job_id)
    if j["status"] != "paused":
        raise HTTPException(409, f"Job {job_id} is {j['status']}; only a paused job can be resumed.")
    if running:
        raise HTTPException(409, f"Job {job_id}'s last run is still stopping; try again shortly.")
    raw = json.loads(j.get("params") or "{}").get("date")
    d = _iso(raw) if raw else None
    if raw and (d is None or d < datetime.now(pytz.timezone(org["timezone"])).date().isoformat()):
        raise HTTPException(409, f"Job {job_id} was for {raw}, which has passed.")
    resume_job_by_id(job_id)
    with engine.connect() as conn:
        return _job_out(conn, _job_row(conn, job_id))


@router.post("/reservations/sync")
async def sync_reservations():
    """Refresh the app's mirror of each account's reservations from CourtReserve. It
    changes only this app's own table, to match what CourtReserve says."""
    from web.routes.reservations import _do_sync
    if not _browser_lock.acquire(blocking=False):
        raise HTTPException(409, "Another availability check or sync is running; try again in a minute.")
    try:
        result = await run_in_threadpool(_do_sync)
    finally:
        _browser_lock.release()
    with engine.connect() as conn:
        scrub = _redactor(conn)
    result["errors"] = [scrub(e)[:300] for e in result.get("errors", [])]
    return result


# ── transfers ────────────────────────────────────────────────────────────────
#
# A job with a probe account books with the probe, then leaves a pending_transfers row:
# the probe holds the court until someone moves it to the main account. Completing one
# is the UI's Transfer button (job_runner.run_transfer, unchanged logic): log in as both,
# cancel the probe's reservation, book the same slot as the main account (3 tries),
# and if that fails have the probe book it back. Between the cancel and the re-book the
# court is open to anyone, so a completed transfer can lose the court.

def _transfer_row(conn, transfer_id: int) -> dict:
    row = conn.execute(pending_transfers.select()
                       .where(pending_transfers.c.id == transfer_id)).fetchone()
    if not row:
        raise HTTPException(404, f"No transfer with id {transfer_id}.")
    return row_to_dict(row)


def _transfer_refusal(conn, t: dict) -> str | None:
    """Why this transfer can't be completed now, or None. Shared by list (as
    `why_not`) and complete (as a 409), so the two never disagree."""
    n = t["id"]
    if t["status"] == "transferring":
        return (f"Transfer {n} is running right now (from the app or the agent). Don't start "
                "it again; check list_transfers in a minute.")
    if t["status"] == "transferred":
        return f"Transfer {n} already moved the court to the main account."
    if t["status"] == "needs_check":
        return (f"Transfer {n}'s cancel on the probe account may have gone through, so where "
                "the court is now is unknown and it MAY BE LOST. A person must check "
                f"CourtReserve and record what they found on the app's page for transfer {n} "
                "before it can be completed.")
    if t["status"] not in ("pending", "held_by_probe"):
        return f"Transfer {n} is {t['status']} and can't be completed."
    if not t.get("reservation_id"):
        return (f"The app has no CourtReserve reservation id for transfer {n}'s court, so it "
                "can't cancel it on the probe account. Move it by hand on CourtReserve.")
    org = conn.execute(organizations.select().where(organizations.c.id == t["org_id"])).fetchone()
    if not org:
        return f"Transfer {n}'s club no longer exists in the app."
    tz = pytz.timezone(org.timezone or "America/Los_Angeles")
    try:
        start = tz.localize(datetime.strptime(f"{t['date']} {t['start_time']}", "%Y-%m-%d %H:%M"))
    except (TypeError, ValueError):
        return f"Transfer {n} has no readable court time."
    if start <= datetime.now(tz):
        return f"Transfer {n}'s court time ({t['date']} {t['start_time']}) has passed."
    for role, acc_id in (("probe", t["probe_account_id"]), ("main", t["main_account_id"])):
        try:
            _club_account(conn, row_to_dict(org), acc_id)
        except HTTPException as e:
            return f"The {role} account: {e.detail}"
    return None


def _transfer_out(conn, t: dict, scrub) -> dict:
    org = conn.execute(organizations.select().where(organizations.c.id == t["org_id"])).fetchone()
    labels = {a.id: _label(a) for a in conn.execute(accounts.select().where(
        accounts.c.id.in_([t["probe_account_id"], t["main_account_id"]])))}
    why_not = _transfer_refusal(conn, t)
    return {
        "id": t["id"], "status": t["status"],
        "club": {"id": t["org_id"], "name": org.name if org else "?"},
        "probe_account": {"id": t["probe_account_id"], "label": labels.get(t["probe_account_id"], "?")},
        "main_account": {"id": t["main_account_id"], "label": labels.get(t["main_account_id"], "?")},
        "date": t["date"], "start_time": t["start_time"],
        "duration_min": t["duration_min"], "court_type": t.get("court_type") or "",
        "job_id": t["job_id"], "auto": bool(t.get("auto")),
        "note": scrub(t.get("note") or "")[:300],
        "created_at": _iso_utc(t.get("created_at")),
        "resolved_at": _iso_utc(t.get("resolved_at")),
        "completable": why_not is None,
        "why_not": why_not,
    }


@router.get("/transfers")
def list_transfers(state: str = "open", limit: int = 20):
    """Courts a probe account holds for a main account. `open` is pending, held by
    the probe after a failed try, or running now; `all` adds transferred and failed."""
    if state not in ("open", "all"):
        raise HTTPException(400, "state is open or all.")
    limit = max(1, min(limit, 100))
    q = pending_transfers.select().order_by(pending_transfers.c.id.desc()).limit(limit)
    if state == "open":
        q = q.where(pending_transfers.c.status.in_(OPEN_TRANSFERS))
    with engine.connect() as conn:
        scrub = _redactor(conn)
        return {"transfers": [_transfer_out(conn, row_to_dict(r), scrub) for r in conn.execute(q)]}


class CompleteTransfer(BaseModel):
    """The court being moved, repeated back: the approval shows a court, not a bare id,
    and a call with a stale or mistaken id is refused instead of moving another court."""
    model_config = ConfigDict(extra="forbid")
    date: str
    start_time: str


def _transfer_result(outcome: dict) -> tuple[str, str, str]:
    """(outcome, court, message) for what run_transfer reported. `court` is where the
    court is now: main, probe, lost or unknown."""
    status = outcome.get("status")
    if status is None and outcome.get("ended"):
        return ("errored", "unknown",
                "The transfer stopped on an internal error in the app and may still be marked as "
                "running there. Check CourtReserve and the app's Jobs page before anything else.")
    if status is None:
        return ("running", "unknown",
                "The transfer started and is still running. It cancels the probe's reservation, "
                "then books the court as the main account. Don't start it again; check "
                "list_transfers in a minute or two.")
    if status == "transferred":
        return ("transferred", "main",
                "Transferred: the probe's reservation was cancelled and the main account booked "
                "the court.")
    if status == "needs_check":
        return ("cancel_uncertain", "unknown",
                "Not transferred. The cancel on the probe account errored after it was submitted, "
                "so it may or may not have gone through, and no booking was tried for the main "
                "account. The court MAY BE LOST. A person must check CourtReserve and record what "
                "they found in the app before this transfer can be tried again.")
    if status == "held_by_probe" and not outcome.get("cancelled"):
        if outcome.get("errored_before_cancel"):
            why = "the app couldn't open a browser or log in"
        elif outcome.get("cancel_error"):
            why = "CourtReserve's cancel page wasn't as expected, and nothing was submitted"
        else:
            why = "CourtReserve didn't confirm the cancel"
        return ("failed_before_cancel", "probe",
                f"Not transferred, and nothing changed: {why}. The probe account still holds "
                "the court.")
    if status == "held_by_probe":
        held = ("" if outcome.get("reservation_id") else
                " Its new reservation id wasn't captured, so this transfer can't be retried here.")
        return ("failed_after_cancel", "probe",
                "Not transferred. The probe's reservation was cancelled, the main account couldn't "
                "book the court, and the probe booked it back: the probe account holds it again, "
                f"as a new reservation.{held}")
    if "may be lost" in (outcome.get("note") or ""):
        return ("failed_after_cancel", "unknown",
                "The transfer errored after the probe's reservation was cancelled. The court MAY "
                "BE LOST. Check CourtReserve.")
    return ("failed_after_cancel", "lost",
            "The court is LOST. The probe's reservation was cancelled, then neither the main "
            "account nor the probe could book it again; someone else most likely took it.")


@router.post("/transfers/{transfer_id}/complete")
def complete_transfer(transfer_id: int, body: CompleteTransfer):
    """Move a court the probe account holds to the main account, through the code the
    UI's Transfer button runs. Refused (409, nothing changed) unless it is pending or
    held by the probe, for this court, with a reservation id, in the future, with both
    accounts linked to the club. Never runs twice: the transfer is claimed in one
    UPDATE, and a running, transferred or failed one is refused."""
    from web import job_runner
    with engine.connect() as conn:
        t = _transfer_row(conn, transfer_id)
        if (_iso(body.date), body.start_time.strip()) != (t["date"], t["start_time"]):
            raise HTTPException(409, f"Transfer {transfer_id} is for {t['date']} {t['start_time']}, "
                                     f"not {body.date} {body.start_time}. Nothing was changed.")
        why_not = _transfer_refusal(conn, t)
    if why_not:
        raise HTTPException(409, f"{why_not} Nothing was changed.")
    if not _browser_lock.acquire(blocking=False):
        raise HTTPException(409, "Another availability check, sync or transfer is running; try "
                                 "again in a minute. Nothing was changed.")
    outcome: dict = {}

    def work():
        try:
            job_runner.run_transfer(transfer_id, outcome)
        finally:
            _browser_lock.release()

    # Its own thread, not this request's: a client that gives up must not stop a
    # transfer between the cancel and the re-book.
    thread = threading.Thread(target=work, name=f"agent-transfer-{transfer_id}", daemon=True)
    thread.start()
    deadline = time.monotonic() + TRANSFER_WAIT_S
    while thread.is_alive() and "status" not in outcome and time.monotonic() < deadline:
        if outcome.get("claimed") is False:
            break
        time.sleep(0.25)
    if not thread.is_alive() and "status" not in outcome:
        outcome["ended"] = True
    if outcome.get("claimed") is False:
        raise HTTPException(409, f"Transfer {transfer_id} was started from elsewhere a moment ago "
                                 "(the app's Transfer button?). Nothing was changed by this call.")
    result, court, message = _transfer_result(outcome)
    with engine.connect() as conn:
        out = _transfer_out(conn, _transfer_row(conn, transfer_id), _redactor(conn))
    return {"outcome": result, "court": court, "message": message, "transfer": out}
