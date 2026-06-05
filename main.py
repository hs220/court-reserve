#!/usr/bin/env python3
import argparse
import os
import random
import smtplib
import subprocess
import sys
import time
import traceback
from datetime import date, datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path

import pytz
from playwright.sync_api import sync_playwright

from config import load_config, default_org_config, SESSION_FILE
from auth import ensure_logged_in, BROWSER_ARGS, USER_AGENT
from booking import (get_available_slots, find_best_slot, book_slot, _org_uses_court_picker,
                     BookingError, BookingWindowError, NoAvailableCourtsError, AlreadyBookedError,
                     CourtSelectionRequiredError, SlotNotBookableError)
from scheduler import wait_until

PT = pytz.timezone("America/Los_Angeles")

# Treat any Playwright timeout (Page.goto, wait_for_selector, APIRequestContext.post,
# etc.) as a transient/retriable condition — the site is just slow or briefly
# unreachable. We'd rather retry than fail a booking/watch job outright.
_NETWORK_ERROR_MARKERS = ["eai_again", "getaddrinfo", "net::", "connection refused", "networkerror", "eof", "timeout", "timed out", "err_timed_out", "err_connection", "err_network", "err_name_not_resolved", "err_internet_disconnected"]

def _is_network_error(exc: Exception) -> bool:
    return any(k in str(exc).lower() for k in _NETWORK_ERROR_MARKERS)


def _ts() -> str:
    return datetime.now(PT).strftime("%Y-%m-%d %H:%M:%S %Z")


def _notify(title: str, message: str) -> None:
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{message}" with title "{title}"'],
            check=False, capture_output=True,
        )
    except Exception:
        pass


def _send_failure_email(subject: str, body: str) -> None:
    """Send a failure notification email via SMTP. Requires NOTIFY_EMAIL and SMTP_PASSWORD env vars."""
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
        print(f"Failure email sent to {to_addr}")
    except Exception as e:
        print(f"Failed to send notification email: {e}")


def watch_and_book(page, probe_page, target_date: date, target_time: str, duration: int,
                   interval: int = 60, timeout_minutes: int = 0, org=None) -> bool:
    """Poll for an available slot and book it. probe_page is used for availability checks
    (may be a different account); page is used for the actual booking."""
    started = time.monotonic()
    while True:
        try:
            slots = get_available_slots(probe_page, target_date, org) if org else get_available_slots(probe_page, target_date)
        except Exception as exc:
            if _is_network_error(exc):
                print(f"[{_ts()}] Network error checking slots — will retry in {interval}s... ({exc})")
                time.sleep(interval)
                continue
            raise

        match = next((s for s in slots if s.start_time == target_time and not s.is_wait_list), None)
        if match:
            print(f"[{_ts()}] Found slot — booking now...")
            booking_url = org.booking_url if org else None
            try:
                success = book_slot(page, booking_url, match, duration_minutes=duration, org=org) if org else book_slot(page, booking_url, match, duration_minutes=duration)
            except NoAvailableCourtsError as e:
                print(f"[{_ts()}] Race condition — courts taken before booking completed, continuing to poll...\n  ({e})")
                success = False
            except CourtSelectionRequiredError as e:
                print(f"[{_ts()}] No court available for the full window (court picker empty); continuing to poll...\n  ({e})")
                success = False
            except SlotNotBookableError as e:
                print(f"[{_ts()}] Slot clicked but no modal (taken in the race) — continuing to poll...\n  ({e})")
                success = False
            except BookingWindowError as e:
                print(f"[{_ts()}] Booking window not open yet: {e}")
                return False
            except AlreadyBookedError as e:
                print(f"[{_ts()}] Already has a reservation — no booking needed: {e}")
                return False
            except BookingError as e:
                print(f"[{_ts()}] Booking rejected: {e}")
                return False
            if success:
                _notify("CourtReserve", f"Court booked for {target_time} on {target_date}")
                return True
            print(f"[{_ts()}] Booking attempt failed (slot may not be fully released yet) — will retry in {interval}s...")

        if timeout_minutes > 0 and (time.monotonic() - started) >= timeout_minutes * 60:
            print(f"[{_ts()}] Timeout after {timeout_minutes}m — no slot found at {target_time} on {target_date}.")
            return False

        jitter = random.uniform(-0.2 * interval, 0.2 * interval)
        wait = interval + jitter
        print(f"[{_ts()}] No slot at {target_time} on {target_date} yet. Next check in {wait:.0f}s...")
        time.sleep(wait)


def cmd_list(args, cfg):
    org = default_org_config()
    target_date = date.fromisoformat(args.date)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed, args=BROWSER_ARGS)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()
        ensure_logged_in(context, page, cfg["email"], cfg["password"], org.booking_url, SESSION_FILE)
        slots = get_available_slots(page, target_date, org)
        if not slots:
            print("No available slots.")
        else:
            print(f"\nAvailable slots on {args.date}:")
            for s in slots:
                wl = " [waitlist]" if s.is_wait_list else ""
                print(f"  {s.start_time}  {s.court_type}  ({s.available_courts} courts){wl}")
        browser.close()


def cmd_book(args, cfg):
    org = default_org_config()
    target_date = date.fromisoformat(args.date)
    # args.time is a list when action="append" is used; None if not provided
    preferred_times = args.time if args.time else cfg.get("preferred_times", [])

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed, args=BROWSER_ARGS)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()

        # Pre-warm: login and land on booking page before the release time
        ensure_logged_in(context, page, cfg["email"], cfg["password"], org.booking_url, SESSION_FILE)

        if args.at:
            wait_until(datetime.fromisoformat(args.at))

        duration = args.duration or cfg.get("default_duration", 60)
        dry_run = getattr(args, 'dry_run', False)

        # Retry loop: keep going until a slot is actually booked. Slots may not
        # appear the instant 12PM ticks, the slot we pick may get snapped up
        # before we click, or the booking click may not register — all of these
        # return without a booking and should be retried with a fresh fetch.
        # Permanent rejections (booking window closed, already booked, no
        # courts) raise BookingError and propagate out immediately — retrying
        # those is pointless.
        ATTEMPTS = 20
        success = False
        # Slots whose cell clicked but never opened a modal (stale feed) — demote
        # so we re-pick a live slot instead of re-clicking the same dead cell.
        dead_starts: set = set()
        for attempt in range(ATTEMPTS):
            last = attempt == ATTEMPTS - 1

            try:
                slots = get_available_slots(page, target_date, org)
            except Exception as exc:
                if not last and _is_network_error(exc):
                    print(f"[{_ts()}] Network error fetching slots, retrying... ({attempt + 1}/{ATTEMPTS}): {exc}")
                    time.sleep(1)
                    continue
                raise

            if not slots:
                if not last:
                    print(f"[{_ts()}] No slots yet, retrying... ({attempt + 1}/{ATTEMPTS})")
                    time.sleep(0.5)
                continue

            # Picker orgs (Santa Clara) can trust the feed's window pre-filter; non-picker
            # orgs (Sunnyvale) must attempt the booking and rely on the server's response.
            window_dur = duration if _org_uses_court_picker(org) else 0
            # Book ONLY from the preferred-times list, in descending preference order;
            # never book a time that isn't on it. A dead preferred slot (no modal) is in
            # dead_starts, so we fall through to the next preferred time. Fallback to
            # arbitrary times is allowed only when no preferred list was given.
            slot = find_best_slot(slots, preferred_times,
                                  allow_fallback=not preferred_times,
                                  duration_minutes=window_dur, exclude_starts=dead_starts)
            if slot is None:
                if not last:
                    print(f"[{_ts()}] No matching slot yet, retrying... ({attempt + 1}/{ATTEMPTS})")
                    time.sleep(0.5)
                continue

            print(f"[{_ts()}] Booking {slot.court_type} at {slot.start_time} on {target_date} for {duration} min...")
            try:
                success = book_slot(page, org.booking_url, slot, duration_minutes=duration, org=org, dry_run=dry_run)
            except Exception as exc:
                if not last and _is_network_error(exc):
                    print(f"[{_ts()}] Network error during booking, retrying... ({attempt + 1}/{ATTEMPTS}): {exc}")
                    continue
                # The booking window may not have flipped open at the exact
                # instant of the 12PM release — retry, it may open momentarily.
                if not last and isinstance(exc, BookingWindowError):
                    print(f"[{_ts()}] Booking window not open yet, retrying... ({attempt + 1}/{ATTEMPTS}): {exc}")
                    time.sleep(0.5)
                    continue
                if not last and isinstance(exc, CourtSelectionRequiredError):
                    print(f"[{_ts()}] No court available for the full window (court picker empty), retrying... ({attempt + 1}/{ATTEMPTS}): {exc}")
                    time.sleep(0.5)
                    continue
                if not last and isinstance(exc, NoAvailableCourtsError):
                    print(f"[{_ts()}] No available courts for the full window, retrying... ({attempt + 1}/{ATTEMPTS}): {exc}")
                    time.sleep(0.5)
                    continue
                if isinstance(exc, SlotNotBookableError):
                    dead_starts.add(slot.start_ms)
                    if not last:
                        print(f"[{_ts()}] Slot {slot.start_time} not bookable (no modal) — "
                              f"demoting and trying another slot... ({attempt + 1}/{ATTEMPTS}): {exc}")
                        continue
                    print(f"[{_ts()}] No bookable slot after {ATTEMPTS} attempts: {exc}")
                    break
                raise

            if success or dry_run:
                break
            if not last:
                print(f"[{_ts()}] Booking attempt failed, retrying... ({attempt + 1}/{ATTEMPTS})")
                time.sleep(0.5)

        if not success and not dry_run:
            print(f"[{_ts()}] Could not book after {ATTEMPTS} attempts.")
        browser.close()
        sys.exit(0 if success else 1)


def cmd_watch(args, cfg):
    org = default_org_config()
    target_date = date.fromisoformat(args.date)
    probe_email = getattr(args, 'probe_email', None) or cfg["email"]
    probe_password = getattr(args, 'probe_password', None) or cfg["password"]
    use_separate_probe = probe_email != cfg["email"]

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed, args=BROWSER_ARGS)

        # Booking context (always)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()
        ensure_logged_in(context, page, cfg["email"], cfg["password"], org.booking_url, SESSION_FILE)

        # Probe context (separate account for slot detection only)
        if use_separate_probe:
            probe_session_file = Path.home() / ".court-reserve-probe-session.json"
            probe_context = browser.new_context(user_agent=USER_AGENT)
            probe_page = probe_context.new_page()
            ensure_logged_in(probe_context, probe_page, probe_email, probe_password, org.booking_url, probe_session_file)
            print(f"watch: probe account {probe_email} for slot detection; booking account {cfg['email']}")
        else:
            probe_page = page

        success = watch_and_book(
            page, probe_page, target_date, args.time, args.duration,
            interval=args.interval, timeout_minutes=args.timeout, org=org,
        )
        browser.close()
    sys.exit(0 if success else 1)


def cmd_book_next(args, cfg):
    today_pt = datetime.now(PT).date()
    days_out = cfg.get("days_out", 7)
    target_date = today_pt + timedelta(days=days_out)
    release_dt = PT.localize(datetime(today_pt.year, today_pt.month, today_pt.day, 12, 0, 0))

    print(f"book-next: targeting {target_date} (today + {days_out} days), release at {release_dt.strftime('%Y-%m-%d %H:%M %Z')}")

    # Start the fetch→click cycle a bit before the official release so we're already
    # mid-attempt the instant slots flip open. Pre-release clicks just raise
    # BookingWindowError and get retried until the window opens.
    RELEASE_LEAD_SECONDS = 30
    args.date = target_date.isoformat()
    args.at = (release_dt - timedelta(seconds=RELEASE_LEAD_SECONDS)).isoformat()

    MAX_RETRIES = 5
    last_exc_tb = None
    for attempt in range(MAX_RETRIES + 1):
        if attempt > 0:
            wait = 30 * attempt
            print(f"[{_ts()}] Attempt {attempt}/{MAX_RETRIES} failed — retrying in {wait}s...")
            time.sleep(wait)
        try:
            print(f"[{_ts()}] book-next attempt {attempt + 1}/{MAX_RETRIES + 1}")
            cmd_book(args, cfg)  # raises SystemExit on completion (success or failure)
        except SystemExit as exc:
            if exc.code == 0:
                raise
            # Booking failed (modal timeout, no slots, etc.) — retry
            last_exc_tb = f"SystemExit({exc.code})"
            print(f"[{_ts()}] Booking attempt {attempt + 1} failed (exit {exc.code}).")
            if attempt >= MAX_RETRIES:
                _send_failure_email(
                    f"Booking failed for {target_date}",
                    f"book-next failed for {target_date} after {MAX_RETRIES + 1} attempt(s).\n\nNo court was booked.",
                )
                raise
            continue
        except Exception as exc:
            last_exc_tb = traceback.format_exc()
            if attempt < MAX_RETRIES and (_is_network_error(exc) or isinstance(exc, BookingWindowError)):
                reason = "Network error" if _is_network_error(exc) else "Booking window not open yet"
                print(f"[{_ts()}] {reason} on attempt {attempt + 1}: {exc}")
                continue
            _send_failure_email(
                f"Booking error for {target_date}",
                f"book-next failed for {target_date} after {attempt + 1} attempt(s).\n\n{last_exc_tb}",
            )
            raise


def main():
    parser = argparse.ArgumentParser(description="CourtReserve tennis court booking tool")
    parser.add_argument("--headed", action="store_true", help="Show browser window (useful for debugging)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="List available slots for a date")
    p_list.add_argument("--date", required=True, help="Target date YYYY-MM-DD")
    p_list.add_argument("--headed", action="store_true", help="Show browser window")

    p_book = sub.add_parser("book", help="Book a court slot")
    p_book.add_argument("--date", required=True, help="Target date YYYY-MM-DD")
    p_book.add_argument("--time", action="append", metavar="TIME",
                        help="Preferred start time HH:MM (24h); repeat for priority list, e.g. --time 10:00 --time 12:00")
    p_book.add_argument("--duration", type=int, help="Duration in minutes (default from config)")
    p_book.add_argument("--at", help="Wait until this datetime before booking (ISO: 'YYYY-MM-DD HH:MM:SS')")
    p_book.add_argument("--headed", action="store_true", help="Show browser window")
    p_book.add_argument("--dry-run", action="store_true", dest="dry_run", help="Open modal and log duration options, but do not click Save")

    p_next = sub.add_parser("book-next", help="Book days_out from today, waiting for today's 12PM PT release")
    p_next.add_argument("--time", action="append", metavar="TIME",
                        help="Preferred start time HH:MM (24h); repeat for priority list, e.g. --time 10:00 --time 12:00")
    p_next.add_argument("--duration", type=int, help="Duration in minutes (default from config)")
    p_next.add_argument("--headed", action="store_true", help="Show browser window")

    p_watch = sub.add_parser("watch", help="Poll until a specific slot opens, then book it")
    p_watch.add_argument("--date", required=True, help="Target date YYYY-MM-DD")
    p_watch.add_argument("--time", required=True, help="Target start time HH:MM (24h)")
    p_watch.add_argument("--duration", required=True, type=int, help="Booking duration in minutes")
    p_watch.add_argument("--interval", type=int, default=60, help="Seconds between polls (default: 60)")
    p_watch.add_argument("--timeout", type=int, default=0, help="Stop after N minutes with no match (default: 0 = infinite)")
    p_watch.add_argument("--headed", action="store_true", help="Show browser window")
    p_watch.add_argument("--probe-email", dest="probe_email", default="",
                        help="Email for probe account used to check availability (default: same as booking account)")
    p_watch.add_argument("--probe-password", dest="probe_password", default="",
                        help="Password for probe account (default: same as booking account)")

    args = parser.parse_args()
    cfg = load_config()

    if args.command == "list":
        cmd_list(args, cfg)
    elif args.command == "book":
        cmd_book(args, cfg)
    elif args.command == "book-next":
        cmd_book_next(args, cfg)
    elif args.command == "watch":
        cmd_watch(args, cfg)


if __name__ == "__main__":
    main()
