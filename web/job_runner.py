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
from web.database import engine, job_runs, jobs, bookings, accounts, organizations, row_to_dict


def _get_account_and_org(account_id: int) -> tuple[dict, dict]:
    with engine.connect() as conn:
        acc = conn.execute(
            accounts.select().where(accounts.c.id == account_id)
        ).fetchone()
        org = conn.execute(
            organizations.select().where(organizations.c.id == acc.org_id)
        ).fetchone()
    return row_to_dict(acc), row_to_dict(org)


def _org_config(org: dict) -> OrgConfig:
    return OrgConfig(
        org_id=org["org_id"],
        scheduler_id=org["scheduler_id"],
        cost_type_id=org["cost_type_id"],
        timezone=org["timezone"],
    )


def _session_file(account: dict) -> Path:
    sf = account.get("session_file", "")
    if sf:
        return Path(sf)
    from web.database import DATA_DIR
    return DATA_DIR / f"session_{account['id']}.json"


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
    Blank target_time = any slot; duration_override=0 = any duration.
    """
    run_id = _start_run(job_id)
    buf = io.StringIO()
    status = "failed"
    try:
        account, org = _get_account_and_org(account_id)
        org_cfg = _org_config(org)
        session_file = _session_file(account)
        preferred_times = [target_time] if target_time else []
        default_duration = duration_override if duration_override > 0 else 0
        days_out = org.get("days_out", 7)
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

        with _capture(buf):
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
                    wait_until((release_dt - timedelta(seconds=8)).replace(tzinfo=None))

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
                    _finish_run(run_id, "failed", buf.getvalue())
                    with engine.begin() as conn:
                        conn.execute(update(jobs).where(jobs.c.id == job_id).values(status="failed"))
                    return

                slot = find_best_slot(slots, preferred_times)
                if slot is None:
                    print("No matching slot found.")
                    browser.close()
                    _finish_run(run_id, "failed", buf.getvalue())
                    with engine.begin() as conn:
                        conn.execute(update(jobs).where(jobs.c.id == job_id).values(status="failed"))
                    return

                success = False
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

        if success:
            _record_booking(run_id, account_id, slot, default_duration)
            status = "success"
        else:
            status = "failed"

    except Exception as exc:
        buf.write(f"\nERROR: {exc}\n")
        status = "failed"

    _finish_run(run_id, status, buf.getvalue())

    job_final = "completed" if status == "success" else "failed"
    with engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(status=job_final))


def run_watch(job_id: int, account_id: int, target_date_iso: str, target_time: str,
              duration: int, interval: int = 60, timeout_minutes: int = 0):
    """
    Poll until a specific slot opens on target_date at target_time, then book it.
    Called by APScheduler in a background thread.
    """
    run_id = _start_run(job_id)
    buf = io.StringIO()
    status = "failed"
    slot = None
    try:
        account, org = _get_account_and_org(account_id)
        org_cfg = _org_config(org)
        session_file = _session_file(account)
        target_date = date.fromisoformat(target_date_iso)

        time_desc = target_time if target_time else "any time"

        with _capture(buf):
            print(f"watch: polling for {time_desc} on {target_date_iso} (interval={interval}s, timeout={timeout_minutes}m)")
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, args=BROWSER_ARGS)
                context = browser.new_context(user_agent=USER_AGENT)
                page = context.new_page()
                ensure_logged_in(context, page, account["email"], account["password"],
                                 org_cfg.booking_url, session_file)

                started = time.monotonic()
                while True:
                    slots = get_available_slots(page, target_date, org_cfg)
                    if target_time:
                        match = next(
                            (s for s in slots if s.start_time == target_time and not s.is_wait_list),
                            None,
                        )
                    else:
                        match = find_best_slot(slots, [])
                    if match:
                        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        print(f"[{ts}] Found slot — booking now...")
                        try:
                            success = book_slot(page, org_cfg.booking_url, match,
                                                duration_minutes=duration, org=org_cfg)
                        except NoAvailableCourtsError as e:
                            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            print(f"[{ts}] Race condition — courts taken before booking completed. Continuing poll...\n  ({e})")
                        except BookingWindowError as e:
                            print(f"Booking window not open yet: {e}")
                            browser.close()
                            status = "failed"
                            break
                        except AlreadyBookedError as e:
                            print(f"Already has a reservation — no booking needed: {e}")
                            browser.close()
                            status = "failed"
                            break
                        except BookingError as e:
                            print(f"Booking rejected: {e}")
                            browser.close()
                            status = "failed"
                            break
                        else:
                            if success:
                                slot = match
                                browser.close()
                                status = "success"
                                break
                            else:
                                # book_slot returned False without a typed error — likely a
                                # transient UI issue (off-screen click, modal timing). Keep polling.
                                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                print(f"[{ts}] Booking attempt did not complete (transient), continuing poll...")

                    if timeout_minutes > 0 and (time.monotonic() - started) >= timeout_minutes * 60:
                        print(f"Timeout after {timeout_minutes}m — no slot found.")
                        browser.close()
                        status = "failed"
                        break

                    jitter = random.uniform(-0.2 * interval, 0.2 * interval)
                    wait = interval + jitter
                    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    print(f"[{ts}] No slot yet. Next check in {wait:.0f}s...")

                    # append partial log while waiting
                    _finish_run(run_id, "running", buf.getvalue())
                    time.sleep(wait)

        if slot is not None:
            _record_booking(run_id, account_id, slot, duration)

    except Exception as exc:
        buf.write(f"\nERROR: {exc}\n")
        status = "failed"

    _finish_run(run_id, status, buf.getvalue())

    # Watch jobs are one-shot: sync job status with the run outcome.
    job_final = "completed" if status == "success" else "failed"
    with engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(status=job_final))
