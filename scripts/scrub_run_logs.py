"""Take session cookies out of run logs stored before the app scrubbed them.

    python scripts/scrub_run_logs.py --dry-run     # counts only, writes nothing
    python scripts/scrub_run_logs.py               # backs up the DB, then scrubs

On the NAS, inside the web container (DATA_DIR=/data there):

    sudo /usr/local/bin/docker exec court-reserve-web-1 python scripts/scrub_run_logs.py --dry-run

Applies log_scrub.scrub -- what the app now runs on everything it stores -- to
`job_runs.log_text`, `pending_transfers.log_text` and `pending_transfers.note`.
Only counts are printed, never text: the point is that nobody reads these values.

It refuses to write while any run is `running`: the app's own writes to a run log can
fail with "database is locked" against this transaction. Pause the jobs whose runs are
in flight (it names them), or run it when none is.

Before writing, it copies the database with SQLite's backup API to a 0600 file next to
it. That copy still holds the cookies; delete it once the scrubbed database looks
right. All updates happen in one transaction, with secure_delete on so the replaced
text isn't left in free pages. Then it truncates the WAL, which still holds the frames
the app wrote the cookies in, retrying for a minute while readers hold it; if it can't,
it says so and exits 3. Running it again changes nothing in the tables (scrub leaves
its own markers alone) and retries the WAL truncation.

None of this recalls a copy made elsewhere: emails already sent, log tails the home
agent read, filesystem snapshots. Ending the CourtReserve sessions does that.

The dry run opens the database read-only, and `immutable` when it has no WAL file, so
it creates no -wal/-shm files. When the app is running, both already exist.
"""

import argparse
import os
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from log_scrub import scrub_counted  # noqa: E402

#: How long to keep retrying a WAL truncation that readers are holding up.
CHECKPOINT_WAIT_S = 60

#: (table, column) pairs that hold run output or text derived from it.
COLUMNS = [("job_runs", "log_text"), ("pending_transfers", "log_text"),
           ("pending_transfers", "note")]


def default_db_path() -> Path:
    data_dir = Path(os.environ.get("DATA_DIR", Path.home() / ".court-reserve-data"))
    return data_dir / "court_reserve.db"


def _columns_present(conn) -> list[tuple[str, str]]:
    present = []
    for table, column in COLUMNS:
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column in cols:
            present.append((table, column))
    return present


def scan(conn) -> tuple[dict, list]:
    """Per (table, column): rows scanned, rows that would change and matches per rule;
    plus the (table, column, id, new_text) updates to make."""
    report, updates = {}, []
    for table, column in _columns_present(conn):
        rows = changed = 0
        matches: Counter = Counter()
        for row_id, text in conn.execute(f"SELECT id, {column} FROM {table}"):
            rows += 1
            new, counts = scrub_counted(text or "")
            if new != (text or ""):
                changed += 1
                matches += counts
                updates.append((table, column, row_id, new))
        report[(table, column)] = {"rows": rows, "changed": changed, "matches": matches}
    return report, updates


def backup(db_path: Path, conn) -> Path:
    """Copy the live database (WAL included) to a new 0600 file beside it."""
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    dest = db_path.with_name(f"{db_path.name}.pre-scrub-{stamp}.bak")
    # Create it 0600 before SQLite opens it, so the copy is never readable by others.
    os.close(os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    out = sqlite3.connect(dest)
    try:
        conn.backup(out)
    finally:
        out.close()
    os.chmod(dest, 0o600)
    return dest


class RunsInFlight(Exception):
    """Runs are `running`; the app may write to their logs at any moment."""

    def __init__(self, job_ids):
        super().__init__(job_ids)
        self.job_ids = job_ids


def running_jobs(conn) -> list[int]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT job_id FROM job_runs WHERE status = 'running' ORDER BY job_id")]


def truncate_wal(conn, wait_s: float = None) -> bool:
    """Checkpoint and truncate the WAL, retrying while a reader holds it. True once the
    WAL is empty."""
    deadline = time.monotonic() + (CHECKPOINT_WAIT_S if wait_s is None else wait_s)
    delay = 0.25
    # TRUNCATE itself waits on the busy handler for readers; keep each wait short so
    # the deadline, not the 30s busy timeout, decides how long this takes.
    conn.execute("PRAGMA busy_timeout=1000")
    while True:
        busy, _, _ = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if busy == 0:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(delay)
        delay = min(delay * 2, 5)


def scrub_db(db_path: Path) -> tuple[dict, Path | None, bool]:
    """Back up, then scrub in one transaction, then truncate the WAL. Returns the
    report, the backup's path (None when nothing needed changing) and whether the WAL
    was truncated. Raises RunsInFlight, writing nothing, while any run is running."""
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA secure_delete=ON")
        if jobs := running_jobs(conn):
            raise RunsInFlight(jobs)
        report, updates = scan(conn)
        if not updates:
            return report, None, truncate_wal(conn)
        dest = backup(db_path, conn)
        # IMMEDIATE takes the write lock now; check and rescan under it, so a run that
        # started or appended since is refused or scrubbed with what it appended.
        conn.execute("BEGIN IMMEDIATE")
        try:
            if jobs := running_jobs(conn):
                raise RunsInFlight(jobs)
            report, updates = scan(conn)
            for table, column, row_id, new in updates:
                conn.execute(f"UPDATE {table} SET {column} = ? WHERE id = ?", (new, row_id))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return report, dest, truncate_wal(conn)
    finally:
        conn.close()


def print_report(report: dict) -> None:
    for (table, column), r in report.items():
        print(f"{table}.{column}: {r['rows']} rows, {r['changed']} with something to remove")
        for rule, n in sorted(r["matches"].items()):
            print(f"    {rule}: {n}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", type=Path, default=None,
                    help="SQLite file (default: $DATA_DIR/court_reserve.db)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print counts only; write nothing")
    args = ap.parse_args(argv)
    db_path = args.db or default_db_path()
    if not db_path.is_file():
        print(f"No database at {db_path}", file=sys.stderr)
        return 1

    if args.dry_run:
        # Without a WAL file there's nothing outside the main file to read, and
        # `immutable` keeps SQLite from creating -wal/-shm just to look.
        has_wal = db_path.with_name(db_path.name + "-wal").exists()
        mode = "mode=ro" if has_wal else "mode=ro&immutable=1"
        conn = sqlite3.connect(f"file:{db_path}?{mode}", uri=True, timeout=30)
        try:
            report, _ = scan(conn)
            jobs = running_jobs(conn)
        finally:
            conn.close()
        print("Dry run: nothing written.")
        print_report(report)
        if jobs:
            print(f"Runs in flight for jobs {', '.join(map(str, jobs))}: the real run will "
                  f"refuse until they stop (pause those jobs first).")
        return 0

    try:
        report, dest, truncated = scrub_db(db_path)
    except RunsInFlight as e:
        print(f"Not scrubbing: runs are in flight for jobs {', '.join(map(str, e.job_ids))}, "
              f"and their log writes could fail against this. Pause those jobs (or wait "
              f"for the runs to end) and run it again. Nothing was written.",
              file=sys.stderr)
        return 2
    print_report(report)
    if dest is None:
        print("Nothing to scrub; no backup made.")
    else:
        print(f"Scrubbed. Backup of the original (still holds the cookies; delete it once "
              f"you're satisfied): {dest}")
    if not truncated:
        print(f"The WAL could not be truncated in {CHECKPOINT_WAIT_S}s: something kept "
              f"reading the database. The -wal file may still hold old log text, cookies "
              f"included. Run this again when the app is idle (or stop it briefly).",
              file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
