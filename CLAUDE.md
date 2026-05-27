# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Setup
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# Copy and fill credentials
cp .env.example .env

# List available slots for a date
python main.py list --date YYYY-MM-DD

# Book a specific slot
python main.py book --date YYYY-MM-DD --time 10:00 --duration 120

# Book today's 12PM release (primary daily command)
python main.py book-next

# Watch for a cancellation slot to open and auto-book it
python main.py watch --date YYYY-MM-DD --time 10:00 --duration 120
python main.py watch --date YYYY-MM-DD --time 10:00 --duration 120 --interval 30 --timeout 120

# Show browser window for debugging
python main.py book-next --headed

# Web UI (local dev)
DATA_DIR=~/.court-reserve-data python web_main.py
# → http://localhost:8080

# Docker — web UI (NAS deployment)
docker compose up web --build
# → http://<nas-ip>:8080

# Docker — one-shot CLI runner
docker build -t court-reserve .
docker run --rm --env-file .env court-reserve python main.py list --date YYYY-MM-DD
docker compose run --rm court-reserve
```

## Architecture

The tool automates CourtReserve's booking UI via Playwright. There is no official API — all interactions reverse-engineer the site's internal endpoints and Kendo UI scheduler.

**Flow for `book-next`:**
1. `main.py:cmd_book_next` — computes target date (`today + days_out`) and release time (12:00 PM PT today), then delegates to `cmd_book`
2. `cmd_book` — launches browser, calls `ensure_logged_in` to pre-warm the session **before** waiting, then calls `wait_until` to hold until noon
3. `booking.py:get_available_slots` — POSTs directly to `ReadConsolidated` API endpoint (bypasses the Kendo UI) to fetch slot data for the target date; retried up to 12× at 0.5s intervals in case courts haven't appeared yet
4. `booking.py:book_slot` — navigates the Kendo Scheduler via `page.evaluate` JS to the target date, clicks the matching time cell, then handles the two-hop AJAX modal (CourtReserve loads a skeleton first, then fires a second request to `reservations.courtreserve.com` for the actual form)
5. `booking.py:find_best_slot` — picks slot by `preferred_times` order, falls back to first non-waitlist slot

**Multi-org support:** `booking.py` now exposes `OrgConfig(org_id, scheduler_id, cost_type_id, timezone)`. The CLI uses `default_org_config()` from `config.py` (reads `config.yaml`). The web UI stores orgs/accounts in SQLite and constructs `OrgConfig` per job.

**Session persistence:** Playwright storage state (cookies) is saved to `~/.court-reserve-session.json` and reused across runs. `ensure_logged_in` checks if the session is still valid before re-logging in.

**Scheduling:**
- macOS: `com.courtreserve.booking.plist` loaded via `launchctl` fires at 11:50 AM PT daily; logs to `/tmp/court-reserve.log`
- Docker/Synology: `docker-compose.yml` + `Dockerfile` with `TZ=America/Los_Angeles` baked in

**Config (`config.yaml`):** `preferred_times`, `default_duration`, `days_out` (7=non-resident, 8=resident). Credentials come from `.env` (`CR_EMAIL`, `CR_PASSWORD`).
