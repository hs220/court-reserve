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

# Run unit tests (stdlib unittest, no extra deps)
python -m unittest discover -p "test_*.py" -v
# Or a single module:
python -m unittest test_booking -v

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
- Passwordless `sudo` on the NAS covers **only** `/usr/local/bin/docker`. Anything
  else (`kill`, `synopkg`, reading `/volume1/@docker/...`) needs an interactive
  `ssh -t` and a password.

## Monitoring

The web container can wedge while still reporting `Up`. In Aug 2026 the unrotated
Synology `db` log driver stopped draining the container's stdout pipe: a thread
blocked in `write()` on fd 1 while holding Python's buffered-writer lock, every
other thread that called `print()` queued behind it, and the uvicorn event loop
stopped serving HTTP. The port still accepted TCP, so nothing looked wrong.

Guards now in `docker-compose.yml`:
- `logging:` — json-file capped at 5 × 10MB, so the log store can't grow unbounded
- `healthcheck:` — a real HTTP GET, since TCP-accept alone can't tell healthy from wedged
- `init: true` — tini reaps Playwright's chromium zombies
- `autoheal` service — restarts `web` when it goes unhealthy. Docker does **not** do
  this itself: `restart: unless-stopped` reacts to process *exit*, and a wedged event
  loop never exits. Deliberately a separate container, since an in-process watchdog
  would be taken down by the fault it's watching for. Opt in with `labels:
  autoheal: "true"`. It mounts `docker.sock` (root-equivalent on the NAS), so the
  image is pinned by digest — re-pin deliberately to upgrade.

`deploy.sh` starts `web autoheal` explicitly; a bare `compose up` would also fire the
one-shot CLI runner.

A job can also fail *while looking healthy*: run #331 polled for 23 hours with every
poll failing and stayed green, because a per-poll error restarts the browser session and
the session restart reset the error streak. Retrying forever is not resilience — a run
that never ends never notifies, so an endlessly-retrying job is indistinguishable from a
working one.

`net_errors.ErrorStreak` is the shared policy for that: `record()` every failed attempt,
`clear()` on every success, and it returns True once the streak has lasted 30 minutes
(min 3 attempts). It is deliberately blind to what the error *is* — a waiver gate, an
expired login, a DNS outage all mean the same thing after half an hour. Both `run_watch`
and the CLI's `watch_and_book` retry loops use it; `run_watch` warns by email at 20
consecutive failures, then ends the run as `failed`, which is what sends the alert. The
trade-off is deliberate: a 30-minute network outage now kills a watch instead of riding
it out. Bounded loops (`run_book_next`'s `MAX_NET_RETRIES`) already ended in a failure
and don't need it.

`get_available_slots` also reports the response's title/text when the feed answers with
HTML, so the log says *what* the site wanted instead of only "Expecting value: line 3".

**Gaps that remain:**
- All failure alerting goes through the app itself, so an app-down event is exactly
  what it cannot report. An out-of-band check (NAS cron curling the endpoint) is the
  smallest thing that closes this. autoheal narrows the window but doesn't tell you
  anything happened.

When the daemon is wedged on a container, `docker kill`/`rm -f` hang and hold its
lock — retrying makes it worse. Restart Container Manager instead:
`sudo synopkg restart ContainerManager` (DSM 7.2.1).

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
