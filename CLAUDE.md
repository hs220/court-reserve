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
# → http://<nas-ip>:7000

# Docker — one-shot CLI runner
docker build -t court-reserve .
docker run --rm --env-file .env court-reserve python main.py list --date YYYY-MM-DD
docker compose run --rm court-reserve

# Deploy to Synology NAS (after initial setup below)
./deploy.sh
```

## Synology NAS Deployment

**Target:** `hsheng@192.168.68.70`, repo at `/volume1/docker/court-reserve/court-reserve/`
**URL:** http://192.168.68.70:7000

**One-time NAS setup:**
```bash
# 1. Enable SSH: DSM → Control Panel → Terminal & SNMP
# 2. Copy SSH public key from your Mac
ssh-copy-id hsheng@192.168.68.70

# 3. SSH in and add GitHub host key
ssh hsheng@192.168.68.70
ssh-keyscan github.com >> ~/.ssh/known_hosts

# 4. Allow passwordless docker (run as admin/root on NAS)
echo 'hsheng ALL=(ALL) NOPASSWD: /usr/local/bin/docker' | sudo tee /etc/sudoers.d/docker-hsheng

# 5. Clone the repo
git clone git@github.com:hs220/court-reserve.git /volume1/docker/court-reserve/court-reserve

# 6. Copy .env with credentials (run from your Mac)
cat .env | ssh hsheng@192.168.68.70 "cat > /volume1/docker/court-reserve/court-reserve/.env"
```

**Ongoing deploys** (from your Mac, in the repo root):
```bash
./deploy.sh   # git pull on NAS + docker compose up web --build -d
```

**Notes:**
- Docker binary is at `/usr/local/bin/docker` (not in default SSH `$PATH`)
- `scp`/`sftp` subsystem is disabled on this NAS; use `cat | ssh` to transfer files
- Data volume (`court_data`) is managed by Docker under `/volume1/@docker/volumes/`

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
