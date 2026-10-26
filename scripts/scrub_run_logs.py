"""Take session cookies out of run logs stored before the app scrubbed them.

    python scripts/scrub_run_logs.py --dry-run     # counts only, writes nothing
    python scripts/scrub_run_logs.py               # backs up the DB, then scrubs

On the NAS, inside the web container (DATA_DIR=/data there):

    sudo /usr/local/bin/docker exec court-reserve-web-1 python scripts/scrub_run_logs.py --dry-run

Applies log_scrub.scrub -- what the app now runs on everything it stores -- to
`job_runs.log_text`, `pending_transfers.log_text` and `pending_transfers.note`.
Only counts are printed, never text: the point is that nobody reads these values.

Before writing, it copies the database with SQLite's backup API to a 0600 file next to
it. That copy still holds the cookies; delete it once the scrubbed database looks
right. All updates happen in one transaction, with secure_delete on so the replaced
text isn't left in free pages. Running it again changes nothing: scrub leaves its own
markers alone.
"""

import argparse
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from log_scrub import scrub_counted  # noqa: E402

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


def scrub_db(db_path: Path) -> tuple[dict, Path | None, bool]:
    """Back up, then scrub in one transaction. Returns the report, the backup's path
    (None when nothing needed changing) and whether the WAL checkpoint completed."""
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA secure_delete=ON")
        report, updates = scan(conn)
        if not updates:
            return report, None, True
        dest = backup(db_path, conn)
        # IMMEDIATE takes the write lock now; rescan under it, so a run that appended
        # since the first scan is scrubbed with what it appended.
        conn.execute("BEGIN IMMEDIATE")
        try:
            report, updates = scan(conn)
            for table, column, row_id, new in updates:
                conn.execute(f"UPDATE {table} SET {column} = ? WHERE id = ?", (new, row_id))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        busy, _, _ = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return report, dest, busy == 0
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
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
        try:
            report, _ = scan(conn)
        finally:
            conn.close()
        print("Dry run: nothing written.")
        print_report(report)
        return 0

    report, dest, checkpointed = scrub_db(db_path)
    print_report(report)
    if dest is None:
        print("Nothing to scrub; no backup made.")
        return 0
    print(f"Scrubbed. Backup of the original (still holds the cookies; delete it once "
          f"you're satisfied): {dest}")
    if not checkpointed:
        print("Note: the WAL checkpoint didn't finish (the app was reading); it will on "
              "the app's next checkpoint.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
