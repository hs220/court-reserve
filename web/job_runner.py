"""Runs booking jobs in background threads; captures stdout and persists to DB."""

import io
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

from booking import (OrgConfig, get_available_slots, find_best_slot, book_slot,
                     BookingError, BookingWindowError, NoAvailableCourtsError, AlreadyBookedError)
from auth import ensure_logged_in, BROWSER_ARGS, USER_AGENT
from scheduler import wait_until
from web.database import engine, job_runs, jobs, bookings, accounts, organizations, row_to_dict, get_days_out

# "apirequestcontext" catches Playwright API request timeouts/failures (e.g. the
# get_available_slots POST) — transient and retriable. Note: a page/selector timeout
# ("page.wait_for_selector: Timeout ...") is deliberately NOT matched, since that's a
# rendering/booking failure, not a network error.
_NETWORK_ERROR_MARKERS = ["eai_again", "getaddrinfo", "net::", "connection refused", "networkerror", "eof", "timed out", "err_timed_out", "err_connection", "err_network", "err_name_not_resolved", "err_internet_disconnected", "apirequestcontext"]

def _is_network_error(exc: Exception) -> bool:
    return any(k in str(exc).lower() for k in _NETWORK_ERROR_MARKERS)


def _send_warning_email(subject: str, body: str) -> None:
    import os, smtplib
    from email.mime.text import MIMEText
    to_addr = os.environ.get("NOTIFY_EMAIL")
    smtp_password = os.environ.get("SMTP_PASSWORD")
    if not to_addr or not smtp_password:
        return
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER", to_addr)
    try:
        msg = MIMEText(body)
        msg["Subject"] = f"[court-reserve] {subject}"
        msg["From"] = smtp_user
        msg["To"] = to_addr
        with smtplib.SMTP(smtp_host, smtp_port) as s:
            s.starttls()
            s.login(smtp_user, smtp_password)
            s.sendmail(smtp_user, [to_addr], msg.as_string())
    except Exception as e:
        print(f"Warning email send failed: {e}")


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
                        wait_until(release_dt - timedelta(seconds=8))

                    slots = None
                    for attempt in range(12):
                        slots = get_available_slots(page, target_date, org_cfg)
                        if slots:
                            break
                        if attempt < 11:
                            print(f"No slots yet, retrying... ({attempt + 1}/12)")
                            time.sleep(0.5)

                    if not slots:
                        print("No available slots — nothing to book.")
                        browser.close()
                        status = "failed"
                    else:
                        slot = find_best_slot(slots, preferred_times, allow_fallback=not preferred_times)
                        if slot is None:
                            print("No matching slot found.")
                            browser.close()
                            status = "failed"
                        else:
                            for window_attempt in range(12):
                                try:
                                    success = book_slot(page, org_cfg.booking_url, slot,
                                                        duration_minutes=default_duration, org=org_cfg)
                                    break
                                except BookingWindowError as e:
                                    if window_attempt < 11:
                                        print(f"Booking window not open yet (attempt {window_attempt + 1}/12), retrying in 5s...")
                                        time.sleep(5)
                                    else:
                                        print(f"Booking window still not open after 12 attempts: {e}")
                                except AlreadyBookedError as e:
                                    print(f"Already has a reservation: {e}")
                                    break
                                except NoAvailableCourtsError as e:
                                    print(f"No available courts: {e}")
                                    break
                                except BookingError as e:
                                    print(f"Booking rejected: {e}")
                                    break
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


def _deadline_timeout_minutes(deadline_mode: str, target_date, target_time: str, org_timezone: str) -> int:
    """Convert deadline_mode to minutes-from-now for the poll timeout. Returns 0 for infinite."""
    if deadline_mode == "infinite" or not target_time:
        return 0
    offset_mins = {"5h10m": 310, "4h10m": 250, "4h": 240, "30m": 30}.get(deadline_mode, 250)
    tz = pytz.timezone(org_timezone)
    hour, minute = map(int, target_time.split(":"))
    court_naive = datetime(target_date.year, target_date.month, target_date.day, hour, minute)
    deadline_dt = tz.localize(court_naive) - timedelta(minutes=offset_mins)
    remaining = (deadline_dt - datetime.now(tz)).total_seconds() / 60
    return max(0, int(remaining))


def run_watch(job_id: int, account_id: int, target_date_iso: str | None, target_time: str,
              duration: int, interval: int = 60, deadline_mode: str = "4h",
              probe_account_id: int | None = None):
    """
    Poll until a specific slot opens on target_date at target_time, then book it.
    If probe_account_id is set, that account is used only for slot-availability checks
    while account_id is used for the actual booking.
    Transient network/timeout errors restart the browser session automatically.
    After 20 consecutive errors a warning email is sent, but the job keeps running.
    """
    run_id = _start_run(job_id)
    buf = io.StringIO()
    status = "failed"
    slot = None
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

        time_desc = target_time if target_time else "any time"
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

        WARNING_THRESHOLD = 20
        consecutive_errors = 0
        done = False  # set True on terminal exits (success / non-retriable error / deadline)

        with _capture(buf):
            if probe_account:
                print(f"watch: probe account '{probe_account.get('label') or probe_account['email']}' for detection; "
                      f"booking account '{account.get('label') or account['email']}'")
            print(f"watch: polling for {time_desc} on {target_date_iso} (interval={interval}s, {deadline_desc}, ~{timeout_minutes}m remaining)")

            started = time.monotonic()

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
                        consecutive_errors = 0  # session established — reset streak

                        while not done:  # poll loop
                            try:
                                slots = get_available_slots(probe_page, target_date, org_cfg)
                            except Exception as exc:
                                if _is_network_error(exc):
                                    # Break inner loop cleanly; outer try/else will restart session.
                                    ts = datetime.now(tz_obj).strftime("%Y-%m-%d %H:%M:%S %Z")
                                    print(f"[{ts}] Network error during poll — restarting session: {exc}")
                                    break
                                raise

                            if target_time:
                                match = next(
                                    (s for s in slots if s.start_time == target_time and not s.is_wait_list),
                                    None,
                                )
                            else:
                                match = find_best_slot(slots, [])

                            if match:
                                ts = datetime.now(tz_obj).strftime("%Y-%m-%d %H:%M:%S %Z")
                                print(f"[{ts}] Found slot — booking now...")
                                try:
                                    success = book_slot(booking_page, org_cfg.booking_url, match,
                                                        duration_minutes=duration, org=org_cfg)
                                except NoAvailableCourtsError as e:
                                    ts = datetime.now(tz_obj).strftime("%Y-%m-%d %H:%M:%S %Z")
                                    print(f"[{ts}] Race condition — courts taken before booking completed. Continuing poll...\n  ({e})")
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
                                    if success:
                                        slot = match
                                        browser.close()
                                        status = "success"
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
                            time.sleep(wait)

                except Exception as exc:
                    if _is_network_error(exc) and not done:
                        consecutive_errors += 1
                        ts = datetime.now(tz_obj).strftime("%Y-%m-%d %H:%M:%S %Z")
                        print(f"[{ts}] Transient error (streak={consecutive_errors}): {exc}")
                        if consecutive_errors >= WARNING_THRESHOLD:
                            _send_warning_email(
                                f"Watch job warning — {consecutive_errors} consecutive errors",
                                f"Watch job #{job_id} polling for {time_desc} on {target_date_iso} "
                                f"has hit {consecutive_errors} consecutive transient errors.\n\n"
                                f"Last error:\n{exc}\n\nThe job is still running.",
                            )
                            consecutive_errors = 0
                        if timeout_minutes > 0 and (time.monotonic() - started) >= timeout_minutes * 60:
                            print(f"[{ts}] Deadline reached during error recovery — stopping.")
                            done = True
                        else:
                            jitter = random.uniform(-0.2 * interval, 0.2 * interval)
                            wait = interval + jitter
                            print(f"[{ts}] Retrying in {wait:.0f}s...")
                            _finish_run(run_id, "running", buf.getvalue())
                            time.sleep(wait)
                    else:
                        raise
                else:
                    # sync_playwright exited without exception: inner poll loop broke due to a
                    # per-poll network error (not a session-setup exception). Restart session.
                    if not done:
                        consecutive_errors += 1
                        ts = datetime.now(tz_obj).strftime("%Y-%m-%d %H:%M:%S %Z")
                        if consecutive_errors >= WARNING_THRESHOLD:
                            _send_warning_email(
                                f"Watch job warning — {consecutive_errors} consecutive errors",
                                f"Watch job #{job_id} polling for {time_desc} on {target_date_iso} "
                                f"has hit {consecutive_errors} consecutive transient errors. Still running.",
                            )
                            consecutive_errors = 0
                        if timeout_minutes > 0 and (time.monotonic() - started) >= timeout_minutes * 60:
                            done = True
                        else:
                            jitter = random.uniform(-0.2 * interval, 0.2 * interval)
                            wait = interval + jitter
                            print(f"[{ts}] Session restarting in {wait:.0f}s...")
                            _finish_run(run_id, "running", buf.getvalue())
                            time.sleep(wait)

        if slot is not None:
            _record_booking(run_id, account_id, slot, duration)

    except Exception as exc:
        buf.write(f"\nERROR: {exc}\n")
        status = "failed"

    _finish_run(run_id, status, buf.getvalue())

    job_final = "completed" if status == "success" else "failed"
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
