"""Runs booking jobs in background threads; captures stdout and persists to DB."""

import io
import json
import sys
import threading
import time
import random
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

# ── Thread-safe stdout capture ────────────────────────────────────────────────
# redirect_stdout() modifies sys.stdout globally and is not thread-safe.
# Instead we install a proxy once and route per-thread output via threading.local.

_tls = threading.local()
_stdout_patch_lock = threading.Lock()
_stdout_patched = False


class _StdoutProxy:
    """Passes all writes through to the real stdout while also copying to per-thread buffers."""
    def __init__(self, real):
        self._real = real

    def write(self, data):
        buf = getattr(_tls, "capture", None)
        if buf is not None and isinstance(data, str):
            buf.write(data)
        self._real.write(data)

    def flush(self):
        self._real.flush()

    def __getattr__(self, name):
        return getattr(self._real, name)


def _ensure_stdout_proxy():
    global _stdout_patched
    with _stdout_patch_lock:
        if not _stdout_patched:
            sys.stdout = _StdoutProxy(sys.stdout)
            _stdout_patched = True


@contextmanager
def _capture(buf: io.StringIO):
    _ensure_stdout_proxy()
    _tls.capture = buf
    try:
        yield buf
    finally:
        _tls.capture = None

import pytz
from playwright.sync_api import sync_playwright
from sqlalchemy import update, insert

from booking import (OrgConfig, Slot, get_available_slots, find_best_slot, book_slot, slot_has_window,
                     _org_uses_court_picker, get_my_reservations, cancel_reservation, match_reservation_id,
                     BookingError, BookingWindowError, NoAvailableCourtsError, AlreadyBookedError,
                     CourtSelectionRequiredError, SlotNotBookableError)

# Start polling/clicking this many seconds BEFORE the official release time. The
# scheduler grid and ReadConsolidated feed can flip open a hair before noon, and
# the extra lead lets us be mid-cycle the instant slots appear.
RELEASE_LEAD_SECONDS = 30
from auth import ensure_logged_in, BROWSER_ARGS, USER_AGENT
from scheduler import wait_until
from web.database import (engine, job_runs, jobs, bookings, accounts, organizations,
                          pending_transfers, row_to_dict, get_days_out)

from net_errors import ErrorStreak, is_network_error as _is_network_error

# ── Cooperative cancellation ──────────────────────────────────────────────────
# A watch job polls for days inside a background thread. APScheduler's remove_job()
# only drops the *trigger*, so editing, pausing or deleting a job that had already
# started left the old thread running: still holding a browser, still polling, and
# still able to book a court for a job that no longer exists. Threads register an
# Event here and check it every cycle; callers set it and the thread winds down.

_cancel_events: dict[int, threading.Event] = {}
_cancel_lock = threading.Lock()


def request_cancel(job_id: int) -> bool:
    """Ask a running job's thread to stop at its next check. Returns True if a thread
    was actually listening. Safe (and a no-op) for jobs that aren't running."""
    with _cancel_lock:
        event = _cancel_events.get(job_id)
    if event is None:
        return False
    event.set()
    return True


def _register_cancel(job_id: int) -> threading.Event:
    """Publish a fresh cancellation Event for a run that is about to start."""
    event = threading.Event()
    with _cancel_lock:
        _cancel_events[job_id] = event
    return event


def _unregister_cancel(job_id: int, event: threading.Event) -> None:
    with _cancel_lock:
        # Only retract our own event. A replacement run for the same job may already
        # have registered its own -- clobbering that would make the new run silently
        # uncancellable, which is the bug this whole mechanism exists to fix.
        if _cancel_events.get(job_id) is event:
            del _cancel_events[job_id]

# A run only fails-and-notifies when something went wrong — often the network itself.
# Without an explicit timeout, smtplib blocks forever on a half-open connection (the
# `read ETIMEDOUT` signature), so the alert never lands and never even errors. Bound
# every attempt and retry a couple of times across a short brownout.
SMTP_TIMEOUT_SECONDS = 30
SMTP_ATTEMPTS = 3
SMTP_RETRY_DELAY_SECONDS = 20


def _send_warning_email(subject: str, body: str) -> str:
    """Best-effort notification email. Returns a short status string describing the
    outcome ("sent", "not configured", or "failed: ...") so callers can record it."""
    import os, smtplib
    from email.mime.text import MIMEText
    to_addr = os.environ.get("NOTIFY_EMAIL")
    smtp_password = os.environ.get("SMTP_PASSWORD")
    if not to_addr or not smtp_password:
        return "not configured (NOTIFY_EMAIL / SMTP_PASSWORD unset)"
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER", to_addr)

    last_error = ""
    for attempt in range(1, SMTP_ATTEMPTS + 1):
        try:
            msg = MIMEText(body)
            msg["Subject"] = f"[court-reserve] {subject}"
            msg["From"] = smtp_user
            msg["To"] = to_addr
            with smtplib.SMTP(smtp_host, smtp_port, timeout=SMTP_TIMEOUT_SECONDS) as s:
                s.starttls()
                s.login(smtp_user, smtp_password)
                s.sendmail(smtp_user, [to_addr], msg.as_string())
            return "sent" if attempt == 1 else f"sent (attempt {attempt}/{SMTP_ATTEMPTS})"
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            print(f"Warning email send failed (attempt {attempt}/{SMTP_ATTEMPTS}): {e}")
            # Only a transient blip is worth waiting out; a bad password never recovers.
            if attempt < SMTP_ATTEMPTS and _is_network_error(e):
                time.sleep(SMTP_RETRY_DELAY_SECONDS)
            else:
                break
    return f"failed: {last_error}"


def _parse_preferred_times(time_val) -> list[str]:
    """Parse time_val which may be a list, comma-separated string, or single string."""
    if isinstance(time_val, list):
        return [t.strip() for t in time_val if t.strip()]
    if time_val and "," in str(time_val):
        return [t.strip() for t in str(time_val).split(",") if t.strip()]
    return [str(time_val).strip()] if time_val else []


def _get_account_and_org(job_id: int, account_id: int) -> tuple[dict, dict]:
    """Look up account and org; org comes from the job's org_id, not the account."""
    with engine.connect() as conn:
        acc = conn.execute(
            accounts.select().where(accounts.c.id == account_id)
        ).fetchone()
        job_row = conn.execute(
            jobs.select().where(jobs.c.id == job_id)
        ).fetchone()
        org = conn.execute(
            organizations.select().where(organizations.c.id == job_row.org_id)
        ).fetchone()
    return row_to_dict(acc), row_to_dict(org)


def _org_config(org: dict) -> OrgConfig:
    return OrgConfig(
        org_id=org["org_id"],
        scheduler_id=org["scheduler_id"],
        cost_type_id=org["cost_type_id"],
        timezone=org["timezone"],
    )


def _session_file(account: dict, org: dict) -> Path:
    """Per-account-per-org session file so different orgs don't clobber each other's cookies."""
    from web.database import DATA_DIR
    return DATA_DIR / f"session_{account['id']}_{org['id']}.json"


def _start_run(job_id: int) -> int:
    with engine.begin() as conn:
        result = conn.execute(insert(job_runs).values(
            job_id=job_id,
            started_at=datetime.utcnow(),
            status="running",
            log_text="",
        ))
        return result.inserted_primary_key[0]


def _finish_run(run_id: int, status: str, log: str):
    with engine.begin() as conn:
        conn.execute(
            update(job_runs)
            .where(job_runs.c.id == run_id)
            .values(finished_at=datetime.utcnow(), status=status, log_text=log)
        )
    if status == "failed":
        _notify_run_failed(run_id, log)


def _append_run_log(run_id: int, line: str) -> None:
    """Append one line to a run's stored log. Used for notes produced after the run's
    stdout capture has already been closed, so they still show up on /runs/{id}."""
    try:
        with engine.begin() as conn:
            conn.execute(
                update(job_runs)
                .where(job_runs.c.id == run_id)
                .values(log_text=job_runs.c.log_text + f"\n{line}")
            )
    except Exception as e:
        print(f"Could not append to run #{run_id} log: {e}")


def _notify_run_failed(run_id: int, log: str) -> None:
    """Email a heads-up when a run ends in the failed state. Best-effort: never let a
    notification problem propagate into the job runner. No-op unless NOTIFY_EMAIL /
    SMTP_PASSWORD are configured (see _send_warning_email).

    The outcome is appended to the run's log. This runs after the caller has already
    snapshotted stdout into the run record, so a plain print() here would only ever
    reach container stdout — invisible on the run page, which is exactly where you go
    looking when an expected alert never arrives."""
    import os
    try:
        with engine.connect() as conn:
            run = conn.execute(job_runs.select().where(job_runs.c.id == run_id)).fetchone()
            if not run:
                return
            job = conn.execute(jobs.select().where(jobs.c.id == run.job_id)).fetchone()
        job_type = job.type if job else "?"
        job_id = job.id if job else run.job_id
        params = (job.params if job else "") or ""
        tail = "\n".join((log or "").strip().splitlines()[-30:]) or "(no log output)"
        base = os.environ.get("WEB_BASE_URL", "").rstrip("/")
        link = f"{base}/runs/{run_id}" if base else f"(open the web UI → run #{run_id})"
        outcome = _send_warning_email(
            f"Job failed: {job_type} (job #{job_id}, run #{run_id})",
            f"A {job_type} job failed.\n\n"
            f"Job:    #{job_id} ({job_type})\n"
            f"Run:    #{run_id}\n"
            f"Params: {params}\n"
            f"Log:    {link}\n\n"
            f"Last log lines:\n{tail}\n",
        )
        _append_run_log(run_id, f"[failure notification] {outcome}")
    except Exception as e:
        print(f"Failure notification error: {e}")
        _append_run_log(run_id, f"[failure notification] errored before sending: {e}")


def _record_booking(run_id: int, account_id: int, slot, duration_min: int):
    with engine.begin() as conn:
        conn.execute(insert(bookings).values(
            job_run_id=run_id,
            account_id=account_id,
            date=slot.start_date,
            start_time=slot.start_time,
            court_type=slot.court_type,
            duration_min=duration_min,
            confirmed_at=datetime.utcnow(),
        ))


# ── probe → main transfer ───────────────────────────────────────────────────────

def _slot_from_fields(date_str: str, start_time: str, duration_min: int, court_type: str, tz: str) -> Slot:
    """Rebuild a Slot from stored fields so book_slot can re-create the reservation.
    book_slot only needs start/end and court_type (not the per-interval court ids)."""
    dt = pytz.timezone(tz).localize(datetime.strptime(f"{date_str} {start_time}", "%Y-%m-%d %H:%M"))
    return Slot(
        court_type=court_type or "Hard",
        start_ms=int(dt.timestamp() * 1000),
        end_ms=int((dt + timedelta(minutes=duration_min)).timestamp() * 1000),
        available_courts=1,
        available_court_ids=[],
        is_wait_list=False,
        timezone=tz,
    )


def _create_pending_transfer(job_id, run_id, org, probe_account, main_account,
                             reservation_id, slot, duration, auto, status="pending", note=""):
    with engine.begin() as conn:
        result = conn.execute(insert(pending_transfers).values(
            job_id=job_id, run_id=run_id, org_id=org["id"],
            probe_account_id=probe_account["id"], main_account_id=main_account["id"],
            reservation_id=reservation_id or "",
            date=slot.start_date, start_time=slot.start_time,
            duration_min=duration, court_type=slot.court_type,
            status=status, auto=bool(auto), note=note,
            created_at=datetime.utcnow(),
        ))
        return result.inserted_primary_key[0]


def _update_pending_transfer(transfer_id, status, note=""):
    resolved = datetime.utcnow() if status in ("transferred", "failed") else None
    with engine.begin() as conn:
        conn.execute(update(pending_transfers).where(pending_transfers.c.id == transfer_id)
                     .values(status=status, note=note, resolved_at=resolved))


def _perform_transfer_with_pages(probe_page, booking_page, org_cfg, reservation_id, slot, duration, log):
    """Cancel the probe's reservation and re-book on the main account, using two
    already-logged-in pages. Returns (status, note):
      transferred    – main holds it
      held_by_probe  – probe still holds it (cancel failed, or main re-book failed but
                       probe re-booked to retain)
      failed         – court lost (cancel ok, main and probe both failed to re-book)"""
    if not reservation_id:
        return "held_by_probe", "No reservation id captured — probe still holds the court; cancel manually."
    try:
        cancelled = cancel_reservation(probe_page, reservation_id, org_cfg)
    except Exception as e:
        log(f"Cancel raised: {e}")
        cancelled = False
    if not cancelled:
        return "held_by_probe", "Cancel failed — probe still holds the court."

    # Re-book on main as fast as possible (the court is exposed during this window).
    main_ok = False
    for attempt in range(3):
        try:
            main_ok = book_slot(booking_page, org_cfg.booking_url, slot, duration_minutes=duration, org=org_cfg)
        except NoAvailableCourtsError:
            main_ok = False
        except Exception as e:
            log(f"Main re-book error (attempt {attempt + 1}): {e}")
            main_ok = False
        if main_ok:
            return "transferred", "Re-booked on the main account."
        time.sleep(0.5)

    # Safety fallback: probe re-books to retain the court rather than lose it.
    log("Main re-book failed — probe re-booking to retain the court...")
    try:
        if book_slot(probe_page, org_cfg.booking_url, slot, duration_minutes=duration, org=org_cfg):
            return "held_by_probe", "Main re-book failed; probe re-booked to retain the court."
    except Exception as e:
        log(f"Probe re-book error: {e}")
    return "failed", "Court lost: main re-book failed and probe could not re-book."


def _persist_transfer_log(transfer_id: int, buf: io.StringIO):
    """Write the captured transfer output to pending_transfers.log_text (leaves
    status/note untouched) so it's viewable in the UI."""
    try:
        with engine.begin() as conn:
            conn.execute(update(pending_transfers)
                         .where(pending_transfers.c.id == transfer_id)
                         .values(log_text=buf.getvalue()))
    except Exception:
        pass


def run_transfer(transfer_id: int):
    """Execute a pending transfer (manual Transfer button): cancel the probe's
    reservation and re-book on the main account. Launches its own browser. All output is
    captured to pending_transfers.log_text so it can be viewed from the UI."""
    buf = io.StringIO()
    with _capture(buf):
        try:
            _run_transfer_inner(transfer_id, buf)
        finally:
            _persist_transfer_log(transfer_id, buf)


def _run_transfer_inner(transfer_id: int, buf: io.StringIO):
    ts = datetime.now(pytz.timezone("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M:%S %Z")
    print(f"[{ts}] Transfer #{transfer_id} starting...")
    # Atomically claim the transfer: flip a runnable transfer (pending/held_by_probe) to
    # 'transferring' in one UPDATE. If nothing was claimed, it's already resolved or being
    # run by another click — bail. (The caller must NOT pre-set 'transferring' itself, or
    # this claim would never match and the worker would no-op — that was the original bug.)
    with engine.begin() as conn:
        claimed = conn.execute(
            update(pending_transfers)
            .where((pending_transfers.c.id == transfer_id) &
                   (pending_transfers.c.status.in_(["pending", "held_by_probe"])))
            .values(status="transferring", note="Transfer started.")
        )
        if claimed.rowcount == 0:
            row = conn.execute(pending_transfers.select()
                               .where(pending_transfers.c.id == transfer_id)).fetchone()
            print(f"Transfer #{transfer_id} is "
                  f"'{row.status if row else 'missing'}' — nothing to do.")
            return
        row = conn.execute(pending_transfers.select()
                           .where(pending_transfers.c.id == transfer_id)).fetchone()
    t = row_to_dict(row)
    _persist_transfer_log(transfer_id, buf)

    with engine.connect() as conn:
        org = row_to_dict(conn.execute(organizations.select().where(organizations.c.id == t["org_id"])).fetchone())
        probe = row_to_dict(conn.execute(accounts.select().where(accounts.c.id == t["probe_account_id"])).fetchone())
        main = row_to_dict(conn.execute(accounts.select().where(accounts.c.id == t["main_account_id"])).fetchone())
    org_cfg = _org_config(org)
    slot = _slot_from_fields(t["date"], t["start_time"], t["duration_min"], t.get("court_type", ""), org_cfg.timezone)

    # (status already set to 'transferring' by the atomic claim above)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=BROWSER_ARGS)
            probe_ctx = browser.new_context(user_agent=USER_AGENT)
            probe_page = probe_ctx.new_page()
            ensure_logged_in(probe_ctx, probe_page, probe["email"], probe["password"],
                             org_cfg.booking_url, _session_file(probe, org))
            main_ctx = browser.new_context(user_agent=USER_AGENT)
            main_page = main_ctx.new_page()
            ensure_logged_in(main_ctx, main_page, main["email"], main["password"],
                             org_cfg.booking_url, _session_file(main, org))
            status, note = _perform_transfer_with_pages(
                probe_page, main_page, org_cfg, t["reservation_id"], slot, t["duration_min"], print)
            browser.close()
    except Exception as exc:
        _update_pending_transfer(transfer_id, "held_by_probe",
                                 f"Transfer errored ({exc}); probe likely still holds the court.")
        _send_warning_email("Court transfer errored",
                            f"Transfer #{transfer_id} for {t['date']} {t['start_time']} errored: {exc}\n"
                            f"The probe account likely still holds the court — check and retry.")
        return

    _update_pending_transfer(transfer_id, status, note)
    if status == "transferred":
        _record_booking(t["run_id"], main["id"], slot, t["duration_min"])
    elif status == "held_by_probe":
        _record_booking(t["run_id"], probe["id"], slot, t["duration_min"])
    _send_warning_email(
        f"Court transfer {status}: {t['date']} {t['start_time']}",
        f"Transfer #{transfer_id} for {t['date']} {t['start_time']} ({t['duration_min']}min): {status}.\n{note}",
    )
    print(f"Transfer #{transfer_id} result: {status} — {note}")


def _handle_probe_secured(job_id, run_id, org, org_cfg, main_account, probe_account,
                          probe_page, booking_page, slot, duration, auto_transfer, tz_obj,
                          captured_reservation_id="") -> str:
    """The probe just secured `slot`. Create a pending transfer, notify, and (if
    auto_transfer) immediately move the court to the main account using the already-open
    probe/main pages. Returns the run status: 'success' | 'pending_transfer' | 'failed'."""
    ts = datetime.now(tz_obj).strftime("%Y-%m-%d %H:%M:%S %Z")
    print(f"[{ts}] Probe secured {slot.start_date} {slot.start_time} — preparing transfer to main...")

    # Prefer the id captured from the save response (most reliable). Otherwise fall back
    # to the bookings list, retrying a few times since a just-created reservation can take
    # a moment to appear there.
    reservation_id = captured_reservation_id or ""
    if reservation_id:
        print(f"[{ts}] Using reservation id from save response: {reservation_id}")
    else:
        for lookup_attempt in range(4):
            try:
                reservations = get_my_reservations(probe_page, org_cfg)
                reservation_id = match_reservation_id(reservations, slot.start_date, slot.start_time) or ""
            except Exception as e:
                print(f"[{ts}] Could not look up the probe's reservation id (attempt {lookup_attempt + 1}/4): {e}")
            if reservation_id:
                print(f"[{ts}] Found reservation id via bookings list: {reservation_id}")
                break
            time.sleep(2)
    if not reservation_id:
        print(f"[{ts}] Warning: no reservation id captured — transfer will need a manual cancel.")

    transfer_id = _create_pending_transfer(
        job_id, run_id, org, probe_account, main_account, reservation_id, slot, duration, auto_transfer)

    probe_label = probe_account.get("label") or probe_account["email"]
    _send_warning_email(
        f"Court secured (pending transfer): {slot.start_date} {slot.start_time}",
        f"The probe account '{probe_label}' booked {slot.start_date} {slot.start_time} "
        f"({duration} min) and is holding it.\n\n"
        + ("Auto-transfer is running now; you'll get a follow-up with the result.\n"
           if auto_transfer else
           "Use the Transfer button on the Jobs page to move it to your main account.\n"))

    if not auto_transfer:
        print(f"[{ts}] Pending transfer #{transfer_id} created — awaiting manual Transfer.")
        return "pending_transfer"

    # Auto-transfer now, reusing the already-warm probe/main pages.
    _update_pending_transfer(transfer_id, "transferring", "Auto-transfer started.")
    try:
        t_status, t_note = _perform_transfer_with_pages(
            probe_page, booking_page, org_cfg, reservation_id, slot, duration,
            log=lambda m: print(f"[{datetime.now(tz_obj).strftime('%Y-%m-%d %H:%M:%S %Z')}] {m}"))
    except Exception as e:
        # Probe already holds the court; leave it recoverable via the Transfer button.
        t_status, t_note = "held_by_probe", f"Auto-transfer errored ({e}); probe still holds the court."
    _update_pending_transfer(transfer_id, t_status, t_note)
    print(f"[{ts}] Auto-transfer #{transfer_id} result: {t_status} — {t_note}")

    if t_status == "transferred":
        _record_booking(run_id, main_account["id"], slot, duration)
        run_status = "success"
    elif t_status == "held_by_probe":
        _record_booking(run_id, probe_account["id"], slot, duration)
        run_status = "pending_transfer"
    else:
        run_status = "failed"

    _send_warning_email(
        f"Court transfer {t_status}: {slot.start_date} {slot.start_time}",
        f"Auto-transfer #{transfer_id} for {slot.start_date} {slot.start_time} ({duration} min): "
        f"{t_status}.\n{t_note}")
    return run_status


def run_book_next(job_id: int, account_id: int, at_iso: str | None = None,
                  target_date_iso: str | None = None, target_time: str = "",
                  duration_override: int = 0):
    """
    Book a court for target_date_iso (or days_out from today if not given).
    target_time may be a comma-separated list of preferred times in priority order.
    duration_override=0 = any duration.
    Retries automatically on transient network errors.
    """
    run_id = _start_run(job_id)
    buf = io.StringIO()
    status = "failed"
    slot = None
    success = False

    account, org = _get_account_and_org(job_id, account_id)
    org_cfg = _org_config(org)
    session_file = _session_file(account, org)
    preferred_times = _parse_preferred_times(target_time)
    default_duration = duration_override if duration_override > 0 else 0
    days_out = get_days_out(account_id, org)
    tz = pytz.timezone(org_cfg.timezone)

    today_local = datetime.now(tz).date()
    if target_date_iso:
        target_date = date.fromisoformat(target_date_iso)
    else:
        target_date = today_local + timedelta(days=days_out)

    release_hour = org.get("release_hour", 12)
    release_minute = org.get("release_minute", 0)
    release_dt = tz.localize(datetime(
        today_local.year, today_local.month, today_local.day,
        release_hour, release_minute, 0
    ))

    MAX_NET_RETRIES = 5
    for net_attempt in range(MAX_NET_RETRIES + 1):
        if net_attempt > 0:
            wait_sec = 30 * net_attempt
            buf.write(f"\nNetwork glitch (attempt {net_attempt}/{MAX_NET_RETRIES}), retrying in {wait_sec}s...\n")
            time.sleep(wait_sec)

        try:
            with _capture(buf):
                if net_attempt > 0:
                    print(f"[Retry {net_attempt}/{MAX_NET_RETRIES}]")
                print(f"book_next: targeting {target_date} (days_out={days_out}), release at {release_dt}")
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True, args=BROWSER_ARGS)
                    context = browser.new_context(user_agent=USER_AGENT)
                    page = context.new_page()

                    ensure_logged_in(context, page, account["email"], account["password"],
                                     org_cfg.booking_url, session_file)

                    if at_iso:
                        wait_until(datetime.fromisoformat(at_iso))
                    elif datetime.now(tz) < release_dt:
                        wait_until(release_dt - timedelta(seconds=RELEASE_LEAD_SECONDS))

                    # Retry the whole fetch→find→book cycle. Slots may not appear
                    # the instant the window opens, the slot we pick may get taken
                    # before we click, or the click may not register a modal (the
                    # failure in issue #2 / run #175) — all transient and worth
                    # retrying with a fresh fetch and re-navigation.
                    BOOK_ATTEMPTS = 20
                    # Slots that weren't bookable THIS pass (no modal / "NONE AVAILABLE").
                    # This is a soft skip, not a permanent ban: a slot can be momentarily
                    # un-bookable (not released yet, or briefly held by another member mid-
                    # booking) and then free up. Once every preferred slot has been skipped
                    # we clear the set and try them all again on fresh grid data, so we keep
                    # racing for the slot until it actually opens.
                    tried: set = set()
                    for book_attempt in range(BOOK_ATTEMPTS):
                        last = book_attempt == BOOK_ATTEMPTS - 1

                        slots = None
                        for attempt in range(12):
                            slots = get_available_slots(page, target_date, org_cfg)
                            if slots:
                                break
                            if attempt < 11:
                                print(f"No slots yet, retrying... ({attempt + 1}/12)")
                                time.sleep(0.5)

                        if not slots:
                            if not last:
                                print(f"No available slots yet — retry {book_attempt + 1}/{BOOK_ATTEMPTS}...")
                                time.sleep(1)
                                continue
                            print("No available slots — nothing to book.")
                            break

                        # Only picker orgs (Santa Clara) can trust the feed's window pre-filter;
                        # non-picker orgs (Sunnyvale) must attempt the booking and rely on the
                        # server's "no available courts" response.
                        window_dur = default_duration if _org_uses_court_picker(org_cfg) else 0
                        # Book ONLY from the user's preferred-times list, in descending
                        # preference order; never book a time that isn't on it. If a
                        # preferred slot is dead (no modal), it's in dead_starts and we
                        # fall through to the next preferred time. Fallback to arbitrary
                        # times is allowed only when no preferred list was given.
                        slot = find_best_slot(slots, preferred_times,
                                              allow_fallback=not preferred_times,
                                              duration_minutes=window_dur, exclude_starts=tried)
                        if slot is None and tried:
                            # Every preferred slot was skipped this pass — clear and retry
                            # them all on fresh data; one may have opened up since.
                            tried = set()
                            slot = find_best_slot(slots, preferred_times,
                                                  allow_fallback=not preferred_times,
                                                  duration_minutes=window_dur, exclude_starts=tried)
                        if slot is None:
                            if not last:
                                print(f"No matching slot yet — retry {book_attempt + 1}/{BOOK_ATTEMPTS}...")
                                time.sleep(1)
                                continue
                            print("No matching slot found.")
                            break

                        try:
                            success = book_slot(page, org_cfg.booking_url, slot,
                                                duration_minutes=default_duration, org=org_cfg)
                        except BookingWindowError as e:
                            if not last:
                                print(f"Booking window not open yet — retry {book_attempt + 1}/{BOOK_ATTEMPTS} in 5s: {e}")
                                time.sleep(5)
                                continue
                            print(f"Booking window still not open after {BOOK_ATTEMPTS} attempts: {e}")
                            break
                        except AlreadyBookedError as e:
                            print(f"Already has a reservation: {e}")
                            break
                        except NoAvailableCourtsError as e:
                            if not last:
                                print(f"No available courts for the full window yet; "
                                      f"retry {book_attempt + 1}/{BOOK_ATTEMPTS}: {e}")
                                time.sleep(1)
                                continue
                            print(f"No available courts after {BOOK_ATTEMPTS} attempts: {e}")
                            break
                        except CourtSelectionRequiredError as e:
                            if not last:
                                print(f"No court available for the full window yet (court picker empty); "
                                      f"retry {book_attempt + 1}/{BOOK_ATTEMPTS}: {e}")
                                time.sleep(1)
                                continue
                            print(f"No court available for the full window after {BOOK_ATTEMPTS} attempts: {e}")
                            break
                        except SlotNotBookableError as e:
                            tried.add(slot.start_ms)
                            if not last:
                                print(f"Slot {slot.start_time} not bookable right now — "
                                      f"skipping this pass, will retry on fresh data; "
                                      f"retry {book_attempt + 1}/{BOOK_ATTEMPTS}: {e}")
                                continue
                            print(f"No bookable slot after {BOOK_ATTEMPTS} attempts: {e}")
                            break
                        except BookingError as e:
                            print(f"Booking rejected: {e}")
                            break

                        if success:
                            break
                        if not last:
                            print(f"Booking attempt failed (transient) — retry {book_attempt + 1}/{BOOK_ATTEMPTS}...")
                            time.sleep(1)

                    browser.close()
                    status = "success" if success else "failed"
            break  # completed (success or expected failure) — do not retry

        except Exception as exc:
            if net_attempt < MAX_NET_RETRIES and _is_network_error(exc):
                buf.write(f"\nNetwork error: {exc}\n")
                continue
            buf.write(f"\nERROR: {exc}\n")
            status = "failed"
            break

    if success and slot is not None:
        _record_booking(run_id, account_id, slot, default_duration)

    _finish_run(run_id, status, buf.getvalue())

    job_final = "completed" if status == "success" else "failed"
    with engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(status=job_final))


_DEADLINE_OFFSETS = {"5h10m": 310, "4h10m": 250, "4h": 240, "30m": 30}


def _time_deadline(deadline_mode: str, target_date, one_time: str, org_timezone: str):
    """Absolute datetime after which `one_time` is no longer worth booking, or None
    for no deadline. `one_time` is a single "HH:MM"."""
    if deadline_mode == "infinite" or not one_time:
        return None
    offset_mins = _DEADLINE_OFFSETS.get(deadline_mode, 250)
    tz = pytz.timezone(org_timezone)
    hour, minute = map(int, one_time.split(":"))
    court_naive = datetime(target_date.year, target_date.month, target_date.day, hour, minute)
    return tz.localize(court_naive) - timedelta(minutes=offset_mins)


def _live_times(deadline_mode: str, target_date, times: list[str], org_timezone: str) -> list[str]:
    """Subset of `times` whose deadline hasn't passed, order preserved.

    With several preferred times each has its own deadline, so they expire one by
    one: watching for 19:00/19:30/20:00 at 4h10m, 19:00 stops being worth booking at
    14:50 while 20:00 is still live until 15:50. Dropping them individually keeps the
    single-time semantics ("never book something already inside the deadline") that
    callers relied on when this only handled one time."""
    if deadline_mode == "infinite" or not times:
        return list(times)
    now = datetime.now(pytz.timezone(org_timezone))
    return [t for t in times
            if (d := _time_deadline(deadline_mode, target_date, t, org_timezone)) is None or d > now]


def _deadline_timeout_minutes(deadline_mode: str, target_date, target_time: str, org_timezone: str) -> int:
    """Convert deadline_mode to minutes-from-now for the poll timeout. Returns 0 for
    infinite. target_time may be a comma-separated list; the job runs until the LAST
    of them expires, with earlier ones pruned as they go (see _live_times)."""
    times = _parse_preferred_times(target_time)
    if deadline_mode == "infinite" or not times:
        return 0
    deadlines = [d for t in times
                 if (d := _time_deadline(deadline_mode, target_date, t, org_timezone)) is not None]
    if not deadlines:
        return 0
    remaining = (max(deadlines) - datetime.now(pytz.timezone(org_timezone))).total_seconds() / 60
    return max(0, int(remaining))


def run_watch(job_id: int, account_id: int, target_date_iso: str | None, target_time: str,
              duration: int, interval: int = 60, deadline_mode: str = "4h",
              probe_account_id: int | None = None, auto_transfer: bool = False):
    """
    Poll until a watched slot opens on target_date, then book it. target_time is one
    "HH:MM" or a comma-separated list in priority order; the first listed time that is
    actually bookable wins, and each time drops out once its own deadline passes.
    If probe_account_id is set (and differs from account_id), that PROBE account does
    the polling AND the booking attempts — keeping repeated/risky Book attempts off the
    main account. When the probe secures the court, a pending_transfers row is created
    and (if auto_transfer) the court is immediately cancelled from the probe and re-booked
    on the main account; otherwise it waits for the manual Transfer button.
    Transient network/timeout errors restart the browser session automatically, but not
    forever: after 20 consecutive failed polls a warning email goes out, and once every
    poll has failed for 30 minutes straight (see net_errors.ErrorStreak) the run ends as
    "failed" so the usual failure notification fires. Retrying an error that never
    clears is indistinguishable, from the outside, from a job that is working.

    Cancellable: editing, pausing or deleting the job sets this run's Event (see
    request_cancel) and the poll loop winds down at its next check instead of
    outliving the job it belongs to.
    """
    run_id = _start_run(job_id)
    buf = io.StringIO()
    status = "failed"
    slot = None
    cancel_event = _register_cancel(job_id)
    try:
        account, org = _get_account_and_org(job_id, account_id)
        org_cfg = _org_config(org)
        session_file = _session_file(account, org)
        if target_date_iso:
            target_date = date.fromisoformat(target_date_iso)
        else:
            tz_local = pytz.timezone(org_cfg.timezone)
            days_out = get_days_out(account_id, org)
            target_date = datetime.now(tz_local).date() + timedelta(days=days_out)

        if probe_account_id and probe_account_id != account_id:
            with engine.connect() as conn:
                probe_row = conn.execute(
                    accounts.select().where(accounts.c.id == probe_account_id)
                ).fetchone()
            probe_account = row_to_dict(probe_row) if probe_row else None
        else:
            probe_account = None

        # When a distinct probe account is configured it does the booking too, and the
        # court is transferred to the main account afterwards (immediately if auto_transfer,
        # else via the Transfer button). Without a probe, the main account books directly.
        using_probe = probe_account is not None

        # target_time may be a single "HH:MM" or a comma-separated list in priority
        # order — one job can watch several candidate start times, which costs nothing
        # extra since each poll already fetches the whole day in one request.
        preferred_times = _parse_preferred_times(target_time)
        time_desc = " / ".join(preferred_times) if preferred_times else "any time"
        timeout_minutes = _deadline_timeout_minutes(deadline_mode, target_date, target_time, org_cfg.timezone)
        deadline_desc = f"stop {deadline_mode} before court" if deadline_mode != "infinite" else "no timeout"

        tz_obj = pytz.timezone(org_cfg.timezone)
        release_hour = org.get("release_hour", 12)
        release_minute = org.get("release_minute", 0)
        days_out_val = get_days_out(account_id, org)
        release_date = target_date - timedelta(days=days_out_val)
        release_dt = tz_obj.localize(datetime(
            release_date.year, release_date.month, release_date.day,
            release_hour, release_minute, 0,
        ))

        # A watch job whose every poll fails is worse than a dead one: it still reads
        # "running" on the runs page, so nothing ever tells you the court isn't being
        # watched. Whatever the polls are failing on, half an hour of solid failure
        # means the job isn't working — end the run so the failure email goes out.
        streak = ErrorStreak()
        last_poll_error = None  # what the inner poll loop broke on, for the outer handler
        done = False  # set True on terminal exits (success / non-retriable error / deadline)

        with _capture(buf):
            if probe_account:
                print(f"watch: probe account '{probe_account.get('label') or probe_account['email']}' for detection; "
                      f"booking account '{account.get('label') or account['email']}'")
            print(f"watch: polling for {time_desc} on {target_date_iso} (interval={interval}s, {deadline_desc}, ~{timeout_minutes}m remaining)")

            started = time.monotonic()

            def note_poll_error(exc, ts) -> bool:
                """Record one failed poll. Returns True when the job should stop: the
                error, whatever it is, has not cleared in half an hour of retrying."""
                if streak.record(exc):
                    print(f"[{ts}] Giving up — retrying is not clearing this: {streak.describe()}")
                    return True
                if streak.should_warn():
                    _send_warning_email(
                        f"Watch job warning — {streak.count} consecutive errors",
                        f"Watch job #{job_id} polling for {time_desc} on {target_date_iso} "
                        f"has hit {streak.describe()}\n\n"
                        f"Still running; it will give up and fail if this keeps up.",
                    )
                return False

            while not done:  # outer session-retry loop
                try:
                    with sync_playwright() as pw:
                        browser = pw.chromium.launch(headless=True, args=BROWSER_ARGS)

                        booking_context = browser.new_context(user_agent=USER_AGENT)
                        booking_page = booking_context.new_page()
                        ensure_logged_in(booking_context, booking_page, account["email"], account["password"],
                                         org_cfg.booking_url, session_file)

                        # Pre-warm if we're within 10 minutes of the booking window opening.
                        time_to_release = (release_dt - datetime.now(tz_obj)).total_seconds()
                        if 0 < time_to_release < 600:
                            print(f"Pre-warming: release at {release_dt.strftime('%H:%M:%S %Z')}, waiting {time_to_release:.0f}s...")
                            wait_until(release_dt - timedelta(seconds=8))

                        if probe_account:
                            probe_session_file = _session_file(probe_account, org)
                            probe_context = browser.new_context(user_agent=USER_AGENT)
                            probe_page = probe_context.new_page()
                            ensure_logged_in(probe_context, probe_page, probe_account["email"], probe_account["password"],
                                             org_cfg.booking_url, probe_session_file)
                        else:
                            probe_page = booking_page

                        probe_label = (probe_account.get("label") or probe_account["email"]) if probe_account else (account.get("label") or account["email"])
                        # NOTE: the streak is deliberately NOT cleared here. Establishing
                        # a session proves nothing — a poll that fails on every session in
                        # turn cleared it here on every restart, which is exactly how run
                        # #331 held a streak of 1 for 23 hours and never warned. Only a
                        # poll that actually returns slots clears it (see below).

                        while not done:  # poll loop
                            if cancel_event.is_set():
                                ts = datetime.now(tz_obj).strftime("%Y-%m-%d %H:%M:%S %Z")
                                print(f"[{ts}] Cancelled — the job was edited, paused or deleted.")
                                browser.close()
                                status = "cancelled"
                                done = True
                                break
                            try:
                                slots = get_available_slots(probe_page, target_date, org_cfg)
                            except Exception as exc:
                                if _is_network_error(exc):
                                    # Break inner loop cleanly; outer try/else will restart session.
                                    ts = datetime.now(tz_obj).strftime("%Y-%m-%d %H:%M:%S %Z")
                                    print(f"[{ts}] Network error during poll — restarting session: {exc}")
                                    last_poll_error = exc
                                    break
                                raise

                            # A poll that came back is the only thing that clears the streak.
                            streak.clear()

                            # For picker orgs (Santa Clara), the feed is a useful cheap
                            # pre-filter: a slot can be open for its 30-min interval yet lack a
                            # contiguous same-court window, and we'd rather not open the modal.
                            # For non-picker orgs (Sunnyvale) the feed is NOT trustworthy enough
                            # to skip on — the only reliable signal is clicking Book and reading
                            # the "no available courts" notice — so always attempt there.
                            window_dur = duration if _org_uses_court_picker(org_cfg) else 0

                            if preferred_times:
                                # Prune times whose own deadline has passed, then take the
                                # earliest-listed one that's actually bookable. allow_fallback
                                # is off: a targeted watch must never grab some other time.
                                active_times = _live_times(deadline_mode, target_date,
                                                           preferred_times, org_cfg.timezone)
                                if not active_times:
                                    ts = datetime.now(tz_obj).strftime("%Y-%m-%d %H:%M:%S %Z")
                                    print(f"[{ts}] All watched times are now inside the "
                                          f"{deadline_mode} deadline — nothing left to wait for.")
                                    browser.close()
                                    status = "failed"
                                    done = True
                                    break
                                match = find_best_slot(slots, active_times, allow_fallback=False,
                                                       duration_minutes=window_dur)
                                if not match and window_dur:
                                    # Distinguish "not open" from "open but unusable" — the
                                    # latter is the confusing one when reading a log later.
                                    blocked = [t for t in active_times
                                               if any(s.start_time == t and not s.is_wait_list
                                                      for s in slots)]
                                    if blocked:
                                        ts = datetime.now(tz_obj).strftime("%Y-%m-%d %H:%M:%S %Z")
                                        print(f"[{ts}] {', '.join(blocked)} open but no contiguous "
                                              f"{duration}-min window (need a single court free the "
                                              f"whole time) — continuing to watch...")
                            else:
                                match = find_best_slot(slots, [], duration_minutes=window_dur)

                            if match:
                                ts = datetime.now(tz_obj).strftime("%Y-%m-%d %H:%M:%S %Z")
                                booker = "probe" if using_probe else "main"
                                print(f"[{ts}] Found slot — booking now (as {booker})...")
                                booker_page = probe_page if using_probe else booking_page
                                book_result: dict = {}
                                try:
                                    success = book_slot(booker_page, org_cfg.booking_url, match,
                                                        duration_minutes=duration, org=org_cfg,
                                                        result=book_result)
                                except NoAvailableCourtsError as e:
                                    ts = datetime.now(tz_obj).strftime("%Y-%m-%d %H:%M:%S %Z")
                                    print(f"[{ts}] No court available for the full window (server reported "
                                          f"no available courts on Save) — or courts were just taken. "
                                          f"Not a failure; continuing to watch...\n  ({e})")
                                except CourtSelectionRequiredError as e:
                                    ts = datetime.now(tz_obj).strftime("%Y-%m-%d %H:%M:%S %Z")
                                    print(f"[{ts}] No court available for the full window yet (court picker empty). "
                                          f"Not a failure; continuing to watch until a court frees up for the whole time.\n  ({e})")
                                except SlotNotBookableError as e:
                                    ts = datetime.now(tz_obj).strftime("%Y-%m-%d %H:%M:%S %Z")
                                    print(f"[{ts}] Slot clicked but no modal (taken in the race). "
                                          f"Not a failure; continuing to watch...\n  ({e})")
                                except BookingWindowError as e:
                                    print(f"Booking window not open yet: {e}")
                                    browser.close()
                                    status = "failed"
                                    done = True
                                except AlreadyBookedError as e:
                                    print(f"Already has a reservation — no booking needed: {e}")
                                    browser.close()
                                    status = "failed"
                                    done = True
                                except BookingError as e:
                                    print(f"Booking rejected: {e}")
                                    browser.close()
                                    status = "failed"
                                    done = True
                                else:
                                    if success and not using_probe:
                                        # Main account booked directly — done.
                                        slot = match
                                        browser.close()
                                        status = "success"
                                        done = True
                                    elif success and using_probe:
                                        # Probe secured the court; create a pending transfer and
                                        # (optionally) move it to the main account immediately.
                                        status = _handle_probe_secured(
                                            job_id, run_id, org, org_cfg, account, probe_account,
                                            probe_page, booking_page, match, duration, auto_transfer, tz_obj,
                                            captured_reservation_id=book_result.get("reservation_id", ""))
                                        browser.close()
                                        done = True
                                    else:
                                        ts = datetime.now(tz_obj).strftime("%Y-%m-%d %H:%M:%S %Z")
                                        print(f"[{ts}] Booking attempt did not complete (transient), continuing poll...")

                            if done:
                                break

                            if timeout_minutes > 0 and (time.monotonic() - started) >= timeout_minutes * 60:
                                ts = datetime.now(tz_obj).strftime("%Y-%m-%d %H:%M:%S %Z")
                                print(f"[{ts}] Timeout after {timeout_minutes}m — no slot found.")
                                browser.close()
                                status = "failed"
                                done = True
                                break

                            jitter = random.uniform(-0.2 * interval, 0.2 * interval)
                            wait = interval + jitter
                            ts = datetime.now(tz_obj).strftime("%Y-%m-%d %H:%M:%S %Z")
                            print(f"[{ts}] No slot yet (checked as {probe_label}). Next check in {wait:.0f}s...")
                            _finish_run(run_id, "running", buf.getvalue())
                            # Wait on the Event, not sleep(): a cancel lands immediately
                            # instead of after up to a full poll interval. The loop head
                            # does the actual teardown.
                            cancel_event.wait(wait)

                except Exception as exc:
                    if _is_network_error(exc) and not done:
                        ts = datetime.now(tz_obj).strftime("%Y-%m-%d %H:%M:%S %Z")
                        give_up = note_poll_error(exc, ts)
                        print(f"[{ts}] Transient error (streak={streak.count}): {exc}")
                        if give_up:
                            status = "failed"
                            done = True
                        elif timeout_minutes > 0 and (time.monotonic() - started) >= timeout_minutes * 60:
                            print(f"[{ts}] Deadline reached during error recovery — stopping.")
                            done = True
                        else:
                            jitter = random.uniform(-0.2 * interval, 0.2 * interval)
                            wait = interval + jitter
                            print(f"[{ts}] Retrying in {wait:.0f}s...")
                            _finish_run(run_id, "running", buf.getvalue())
                            if cancel_event.wait(wait):
                                # Browser is already gone on this path; nothing to close.
                                print(f"[{ts}] Cancelled during error recovery — stopping.")
                                status = "cancelled"
                                done = True
                    else:
                        raise
                else:
                    # sync_playwright exited without exception: inner poll loop broke due to a
                    # per-poll network error (not a session-setup exception). Restart session.
                    if not done:
                        ts = datetime.now(tz_obj).strftime("%Y-%m-%d %H:%M:%S %Z")
                        if note_poll_error(last_poll_error, ts):
                            status = "failed"
                            done = True
                        elif timeout_minutes > 0 and (time.monotonic() - started) >= timeout_minutes * 60:
                            done = True
                        else:
                            jitter = random.uniform(-0.2 * interval, 0.2 * interval)
                            wait = interval + jitter
                            print(f"[{ts}] Session restarting in {wait:.0f}s (failed polls: {streak.count})...")
                            _finish_run(run_id, "running", buf.getvalue())
                            if cancel_event.wait(wait):
                                print(f"[{ts}] Cancelled before session restart — stopping.")
                                status = "cancelled"
                                done = True

        if slot is not None:
            _record_booking(run_id, account_id, slot, duration)

    except Exception as exc:
        buf.write(f"\nERROR: {exc}\n")
        status = "failed"
    finally:
        _unregister_cancel(job_id, cancel_event)

    _finish_run(run_id, status, buf.getvalue())

    # A cancelled run must not touch jobs.status. Whoever cancelled it owns that now:
    # an edit has already set the row back to "active" and scheduled a replacement run,
    # a pause set "paused", and a delete removed the row entirely. Writing "failed"
    # here would clobber their state from a thread that is on its way out.
    if status == "cancelled":
        return

    # pending_transfer counts as a completed watch — we secured the court (the probe
    # holds it / it's being moved to main); the watch itself shouldn't reschedule.
    job_final = "completed" if status in ("success", "pending_transfer") else "failed"
    with engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(status=job_final))


def run_recurrent_book_next(job_id: int, account_id: int):
    """Create a one-shot book_next child job for the upcoming target date.
    Called 1 day before the release date; target_date = today + days_out + 1."""
    run_id = _start_run(job_id)
    buf = io.StringIO()
    status = "failed"
    try:
        account, org = _get_account_and_org(job_id, account_id)
        tz = pytz.timezone(org["timezone"])
        days_out = get_days_out(account_id, org)
        today_local = datetime.now(tz).date()
        target_date = today_local + timedelta(days=days_out + 1)

        with engine.connect() as conn:
            job_row = conn.execute(jobs.select().where(jobs.c.id == job_id)).fetchone()
        params = json.loads(job_row.params or "{}")

        release_date = today_local + timedelta(days=1)
        release_hour = org.get("release_hour", 12)
        release_minute = org.get("release_minute", 0)
        fire_dt = tz.localize(datetime(
            release_date.year, release_date.month, release_date.day,
            release_hour, release_minute, 0,
        )) - timedelta(minutes=2)
        run_at = fire_dt.strftime("%Y-%m-%dT%H:%M") if fire_dt > datetime.now(tz) else ""

        child_params = {
            "date": target_date.isoformat(),
            "time": params.get("time", ""),
            "duration": params.get("duration", 0),
            "run_at": run_at,
        }
        with engine.begin() as conn:
            result = conn.execute(jobs.insert().values(
                account_id=account_id,
                org_id=job_row.org_id,
                type="book_next",
                params=json.dumps(child_params),
                status="active",
                cron_expr="",
            ))
            child_id = result.inserted_primary_key[0]

        from web import apscheduler_setup as _aps
        _aps.schedule_book_next(child_id, account_id, child_params)

        buf.write(f"Created book_next job #{child_id} for {target_date}"
                  f" (fires {run_at or 'at org release time'})\n")
        status = "success"
    except Exception as exc:
        buf.write(f"ERROR: {exc}\n")

    _finish_run(run_id, status, buf.getvalue())


def run_recurrent_watch(job_id: int, account_id: int):
    """Create a one-shot watch child job for the target date.
    Called 2min before release on the release date; target_date = today + days_out."""
    run_id = _start_run(job_id)
    buf = io.StringIO()
    status = "failed"
    try:
        account, org = _get_account_and_org(job_id, account_id)
        tz = pytz.timezone(org["timezone"])
        days_out = get_days_out(account_id, org)
        today_local = datetime.now(tz).date()
        target_date = today_local + timedelta(days=days_out)

        with engine.connect() as conn:
            job_row = conn.execute(jobs.select().where(jobs.c.id == job_id)).fetchone()
        params = json.loads(job_row.params or "{}")
        probe_id = params.get("probe_account_id")

        child_params = {
            "date": target_date.isoformat(),
            "time": params.get("time", ""),
            "duration": params.get("duration", 120),
            "interval": params.get("interval", 60),
            "deadline_mode": params.get("deadline_mode", "4h"),
            "probe_account_id": probe_id,
            "auto_transfer": bool(params.get("auto_transfer", False)),
            "run_at": "",  # start immediately — window is already open
        }
        with engine.begin() as conn:
            result = conn.execute(jobs.insert().values(
                account_id=account_id,
                org_id=job_row.org_id,
                type="watch",
                params=json.dumps(child_params),
                status="active",
                cron_expr="",
            ))
            child_id = result.inserted_primary_key[0]

        from web import apscheduler_setup as _aps
        _aps.schedule_watch(child_id, account_id, child_params)

        buf.write(f"Created watch job #{child_id} for {target_date}\n")
        status = "success"
    except Exception as exc:
        buf.write(f"ERROR: {exc}\n")

    _finish_run(run_id, status, buf.getvalue())
