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
from booking import get_available_slots, find_best_slot, book_slot
from scheduler import wait_until

PT = pytz.timezone("America/Los_Angeles")

_NETWORK_ERROR_MARKERS = ["eai_again", "getaddrinfo", "net::", "connection refused", "networkerror", "eof", "timeout"]

def _is_network_error(exc: Exception) -> bool:
    return any(k in str(exc).lower() for k in _NETWORK_ERROR_MARKERS)


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
        slots = get_available_slots(probe_page, target_date, org) if org else get_available_slots(probe_page, target_date)
        match = next((s for s in slots if s.start_time == target_time and not s.is_wait_list), None)
        if match:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{ts}] Found slot — booking now...")
            booking_url = org.booking_url if org else None
            success = book_slot(page, booking_url, match, duration_minutes=duration, org=org) if org else book_slot(page, booking_url, match, duration_minutes=duration)
            if success:
                _notify("CourtReserve", f"Court booked for {target_time} on {target_date}")
                return True
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{ts}] Booking attempt failed (slot may not be fully released yet) — will retry in {interval}s...")

        if timeout_minutes > 0 and (time.monotonic() - started) >= timeout_minutes * 60:
            print(f"Timeout after {timeout_minutes}m — no slot found at {target_time} on {target_date}.")
            return False

        jitter = random.uniform(-0.2 * interval, 0.2 * interval)
        wait = interval + jitter
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] No slot at {target_time} on {target_date} yet. Next check in {wait:.0f}s...")
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

        # Retry loop: slots may not appear the instant 12PM ticks
        slots = None
        for attempt in range(12):
            slots = get_available_slots(page, target_date, org)
            if slots:
                break
            if attempt < 11:
                print(f"No slots yet, retrying... ({attempt + 1}/12)")
                time.sleep(0.5)

        if not slots:
            print("No available slots — nothing to book.")
            browser.close()
            sys.exit(1)

        slot = find_best_slot(slots, preferred_times)
        if slot is None:
            print("No matching slot found.")
            browser.close()
            sys.exit(1)

        duration = args.duration or cfg.get("default_duration", 60)
        success = book_slot(page, org.booking_url, slot, duration_minutes=duration, org=org, dry_run=getattr(args, 'dry_run', False))
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

    args.date = target_date.isoformat()
    args.at = release_dt.strftime("%Y-%m-%d %H:%M:%S")

    MAX_RETRIES = 5
    for attempt in range(MAX_RETRIES + 1):
        if attempt > 0:
            wait = 30 * attempt
            print(f"Network error on attempt {attempt}/{MAX_RETRIES}, retrying in {wait}s...")
            time.sleep(wait)
        try:
            cmd_book(args, cfg)  # raises SystemExit on completion (success or failure)
        except SystemExit as exc:
            if exc.code != 0:
                _send_failure_email(
                    f"Booking failed for {target_date}",
                    f"book-next exited with failure for {target_date}.\n\nNo court was booked.",
                )
            raise
        except Exception as exc:
            if attempt < MAX_RETRIES and _is_network_error(exc):
                print(f"Network error: {exc}")
                continue
            _send_failure_email(
                f"Booking error for {target_date}",
                f"book-next failed for {target_date} after {attempt + 1} attempt(s).\n\n{traceback.format_exc()}",
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
