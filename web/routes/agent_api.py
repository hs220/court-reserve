"""A narrow JSON API for the home agent (hs220/home-agent, connector `courtreserve`).

The agent reaches this over the `court-reserve-share` Docker network as
`court-reserve-web:8080`; nothing else of home-agent's can. It gets exactly what its
tools need and nothing more:

- read: jobs and their runs, upcoming reservations the app knows about, clubs and
  account labels, and a live availability check for one date;
- write: create a one-shot booking or watch job, pause or resume a job, and re-sync
  the reservations mirror from CourtReserve.

What is deliberately absent: deleting, editing, restarting or run-now on a job;
cancelling or transferring a reservation; anything about accounts or organizations
beyond their names. A missing endpoint is the only guarantee that survives a
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
from datetime import date as date_cls, datetime, timedelta, timezone

import pytz
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text
from starlette.concurrency import run_in_threadpool

from web import apscheduler_setup
from web.database import account_orgs, accounts, engine, job_runs, jobs, organizations, row_to_dict

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

#: One browser at a time for agent-initiated work. Each availability check or sync
#: launches Chromium and may log in; a watcher asking in a loop must not be able to
#: stack those up on the NAS next to the watch jobs that matter.
_browser_lock = threading.Lock()


def _require_token(authorization: str = Header(default="")) -> None:
    want = os.environ.get(TOKEN_HASH_ENV, "").strip().lower()
    if not want:
        raise HTTPException(503, f"The agent API is off: {TOKEN_HASH_ENV} is not set.")
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


def _redactor(conn):
    """A function that makes run text safe to return: secrets out, emails to labels."""
    rows = conn.execute(select(accounts)).fetchall()
    secrets = {r.password for r in rows if r.password}
    secrets |= {os.environ[k] for k in ENV_SECRETS if os.environ.get(k)}
    # Longest first, so a password containing another is replaced whole.
    ordered = sorted(secrets, key=len, reverse=True)
    emails = {r.email: _label(r) for r in rows if r.email}

    def scrub(s: str) -> str:
        for secret in ordered:
            s = s.replace(secret, "[redacted]")
        for email, label in emails.items():
            s = s.replace(email, label)
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


def _pick_account(conn, org_id: int, account_id: int | None) -> dict:
    if account_id is not None:
        row = conn.execute(accounts.select().where(accounts.c.id == account_id)).fetchone()
        if not row:
            raise HTTPException(404, f"No account with id {account_id}.")
        return row_to_dict(row)
    row = conn.execute(
        select(accounts).join(account_orgs, account_orgs.c.account_id == accounts.c.id)
        .where(account_orgs.c.org_id == org_id).order_by(accounts.c.id).limit(1)
    ).fetchone() or conn.execute(accounts.select().order_by(accounts.c.id).limit(1)).fetchone()
    if not row:
        raise HTTPException(409, "No CourtReserve account is set up in the app.")
    return row_to_dict(row)


@router.get("/availability")
async def availability(org_id: int, date: str, account_id: int | None = None):
    try:
        target = date_cls.fromisoformat(date)
    except ValueError:
        raise HTTPException(400, "date is YYYY-MM-DD.")
    with engine.connect() as conn:
        org = _org(conn, org_id)
        account = _pick_account(conn, org_id, account_id)
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


def _validate(conn, body: NewBooking, job_type: str) -> tuple[dict, list[str]]:
    org = _org(conn, body.org_id)
    if not conn.execute(accounts.select().where(accounts.c.id == body.account_id)).fetchone():
        raise HTTPException(404, f"No account with id {body.account_id}.")
    tz = pytz.timezone(org["timezone"])
    now = datetime.now(tz)
    try:
        d = date_cls.fromisoformat(body.date)
    except ValueError:
        raise HTTPException(400, "date is YYYY-MM-DD.")
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
    for r in conn.execute(jobs.select().where(
            (jobs.c.type == job_type) & (jobs.c.account_id == body.account_id) &
            (jobs.c.org_id == body.org_id) & jobs.c.status.in_(["active", "paused"]))):
        if json.loads(r.params or "{}").get("date") == body.date:
            raise HTTPException(409, f"Job {r.id} already covers {body.date} for this account "
                                     f"and club ({r.status}).")
    return org, times


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
        org, times = _validate(conn, body, "book_next")
    job_id = create_book_next_job(body.account_id, body.org_id, body.date, ",".join(times),
                                  body.duration_min)
    return _created(job_id, org)


@router.post("/jobs/watch")
def create_watch(body: NewWatch):
    """A watch job: polls for a cancellation at one of `times` and books it when one opens."""
    from web.routes.jobs import create_watch_job
    if body.stop_before not in STOP_BEFORE:
        raise HTTPException(400, f"stop_before is one of {', '.join(STOP_BEFORE)}.")
    with engine.connect() as conn:
        org, times = _validate(conn, body, "watch")
    job_id = create_watch_job(body.account_id, body.org_id, body.date, ",".join(times),
                              body.duration_min, WATCH_INTERVAL_S, body.stop_before)
    return _created(job_id, org)


@router.post("/jobs/{job_id}/pause")
def pause(job_id: int):
    from web.routes.jobs import pause_job_by_id
    with engine.connect() as conn:
        j = _job_row(conn, job_id)
    if j["status"] != "active":
        raise HTTPException(409, f"Job {job_id} is {j['status']}; only an active job can be paused.")
    pause_job_by_id(job_id)
    with engine.connect() as conn:
        return _job_out(conn, _job_row(conn, job_id))


@router.post("/jobs/{job_id}/resume")
def resume(job_id: int):
    from web.routes.jobs import resume_job_by_id
    with engine.connect() as conn:
        j = _job_row(conn, job_id)
        org = _org(conn, j["org_id"])
    if j["status"] != "paused":
        raise HTTPException(409, f"Job {job_id} is {j['status']}; only a paused job can be resumed.")
    d = json.loads(j.get("params") or "{}").get("date")
    if d and d < datetime.now(pytz.timezone(org["timezone"])).date().isoformat():
        raise HTTPException(409, f"Job {job_id} was for {d}, which has passed.")
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
