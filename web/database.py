import json
import os
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy import (
    Column, Integer, String, Text, DateTime, Boolean, ForeignKey,
    MetaData, Table, UniqueConstraint,
)

DATA_DIR = Path(os.environ.get("DATA_DIR", Path.home() / ".court-reserve-data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "court_reserve.db"
engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False, "timeout": 30},
)

@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_conn, _):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")   # concurrent readers+writers
    cur.execute("PRAGMA busy_timeout=30000") # retry for up to 30s on lock
    cur.close()
metadata = MetaData()

organizations = Table("organizations", metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String, nullable=False),
    Column("org_id", String, nullable=False),
    Column("scheduler_id", String, nullable=False),
    Column("cost_type_id", String, nullable=False),
    Column("timezone", String, nullable=False, default="America/Los_Angeles"),
    Column("resident_days_out", Integer, default=7),
    Column("nonresident_days_out", Integer, default=7),
    Column("release_hour", Integer, default=12),     # local hour when slots open
    Column("release_minute", Integer, default=0),
)

account_orgs = Table("account_orgs", metadata,
    Column("id", Integer, primary_key=True),
    Column("account_id", Integer, ForeignKey("accounts.id"), nullable=False),
    Column("org_id", Integer, ForeignKey("organizations.id"), nullable=False),
    Column("is_resident", Boolean, default=False),
    UniqueConstraint("account_id", "org_id"),
)

accounts = Table("accounts", metadata,
    Column("id", Integer, primary_key=True),
    Column("org_id", Integer, ForeignKey("organizations.id"), nullable=True),  # nullable: accounts are org-independent
    Column("label", String, default=""),             # friendly name e.g. "Alice"
    Column("email", String, nullable=False),
    Column("password", String, nullable=False),
    Column("session_file", String, default=""),
)

jobs = Table("jobs", metadata,
    Column("id", Integer, primary_key=True),
    Column("account_id", Integer, ForeignKey("accounts.id"), nullable=False),
    Column("org_id", Integer, ForeignKey("organizations.id"), nullable=False),
    Column("type", String, nullable=False),          # "book_next" | "watch"
    Column("params", Text, default="{}"),            # JSON
    Column("status", String, default="active"),      # active | paused | completed | failed
    Column("cron_expr", String, default=""),         # for book_next: "50 11 * * *" (cron)
    Column("next_run_at", DateTime, nullable=True),
    Column("created_at", DateTime, default=datetime.utcnow),
    Column("apscheduler_id", String, default=""),    # APScheduler job id
)

job_runs = Table("job_runs", metadata,
    Column("id", Integer, primary_key=True),
    Column("job_id", Integer, ForeignKey("jobs.id"), nullable=False),
    Column("started_at", DateTime, default=datetime.utcnow),
    Column("finished_at", DateTime, nullable=True),
    Column("status", String, default="running"),     # running | success | failed
    Column("log_text", Text, default=""),
)

bookings = Table("bookings", metadata,
    Column("id", Integer, primary_key=True),
    Column("job_run_id", Integer, ForeignKey("job_runs.id"), nullable=True),
    Column("account_id", Integer, ForeignKey("accounts.id"), nullable=False),
    Column("date", String, nullable=False),          # YYYY-MM-DD
    Column("start_time", String, nullable=False),    # HH:MM
    Column("court_type", String, default=""),
    Column("duration_min", Integer, default=120),
    Column("confirmed_at", DateTime, default=datetime.utcnow),
)


pending_transfers = Table("pending_transfers", metadata,
    Column("id", Integer, primary_key=True),
    Column("job_id", Integer, ForeignKey("jobs.id"), nullable=False),
    Column("run_id", Integer, ForeignKey("job_runs.id"), nullable=True),
    Column("org_id", Integer, ForeignKey("organizations.id"), nullable=False),
    Column("probe_account_id", Integer, ForeignKey("accounts.id"), nullable=False),
    Column("main_account_id", Integer, ForeignKey("accounts.id"), nullable=False),
    Column("reservation_id", String, default=""),     # probe's reservation, to cancel
    Column("date", String, nullable=False),            # YYYY-MM-DD
    Column("start_time", String, nullable=False),      # HH:MM
    Column("duration_min", Integer, default=120),
    Column("court_type", String, default=""),
    # pending: held by probe, awaiting transfer; transferring: in progress;
    # transferred: now on main; held_by_probe: transfer failed, probe still holds it;
    # failed: court lost
    Column("status", String, default="pending"),
    Column("auto", Boolean, default=False),            # auto-transfer requested
    Column("note", Text, default=""),                  # last status detail
    Column("created_at", DateTime, default=datetime.utcnow),
    Column("resolved_at", DateTime, nullable=True),
)


bug_reports = Table("bug_reports", metadata,
    Column("id", Integer, primary_key=True),
    Column("title", String, nullable=False),
    Column("description", Text, default=""),
    Column("severity", String, default="medium"),     # low | medium | high
    Column("reporter", String, default=""),            # optional name/email
    Column("status", String, default="open"),          # open | resolved
    Column("created_at", DateTime, default=datetime.utcnow),
    Column("resolved_at", DateTime, nullable=True),
    Column("gh_issue_number", Integer, nullable=True),  # GitHub issue number, if filed
    Column("gh_issue_url", String, default=""),
)


SEED_ORGS = [
    dict(name="Lifetime Sunnyvale",   org_id="13233", scheduler_id="16983",  cost_type_id="141205"),
    dict(name="Lifetime Santa Clara", org_id="13234", scheduler_id="16995",  cost_type_id="141206"),
    dict(name="Lifetime Cupertino",   org_id="13229", scheduler_id="18246",  cost_type_id="141203"),
]


def init_db():
    metadata.create_all(engine)
    _migrate_accounts_org_optional()
    _migrate_orgs_resident_days_out()
    _migrate_bug_reports_gh_columns()
    _seed_orgs()


def _migrate_accounts_org_optional():
    """Make accounts.org_id nullable if it isn't already (SQLite requires table recreation)."""
    import re
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='accounts'"
        )).fetchone()
        if not row:
            return
        if not re.search(r'org_id[^,)]*NOT NULL', row[0] or "", re.IGNORECASE):
            return  # already nullable
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE accounts_new (
                id INTEGER NOT NULL PRIMARY KEY,
                org_id INTEGER REFERENCES organizations (id),
                label VARCHAR DEFAULT '',
                email VARCHAR NOT NULL,
                password VARCHAR NOT NULL,
                session_file VARCHAR DEFAULT ''
            )
        """))
        conn.execute(text("INSERT INTO accounts_new SELECT * FROM accounts"))
        conn.execute(text("DROP TABLE accounts"))
        conn.execute(text("ALTER TABLE accounts_new RENAME TO accounts"))


def _migrate_orgs_resident_days_out():
    """Add resident_days_out / nonresident_days_out if missing, seeding from days_out."""
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='organizations'"
        )).fetchone()
        if not row:
            return
        sql = row[0] or ""
        if "resident_days_out" in sql:
            return
        has_days_out = "days_out" in sql
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE organizations ADD COLUMN resident_days_out INTEGER DEFAULT 7"))
        conn.execute(text("ALTER TABLE organizations ADD COLUMN nonresident_days_out INTEGER DEFAULT 7"))
        if has_days_out:
            conn.execute(text(
                "UPDATE organizations SET resident_days_out = days_out, nonresident_days_out = days_out"
            ))


def _migrate_bug_reports_gh_columns():
    """Add GitHub-issue tracking columns to bug_reports if missing."""
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='bug_reports'"
        )).fetchone()
        if not row:
            return
        sql = row[0] or ""
        if "gh_issue_number" in sql:
            return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE bug_reports ADD COLUMN gh_issue_number INTEGER"))
        conn.execute(text("ALTER TABLE bug_reports ADD COLUMN gh_issue_url VARCHAR DEFAULT ''"))


def _seed_orgs():
    with engine.connect() as conn:
        if conn.execute(organizations.select().limit(1)).fetchone():
            return  # already populated
    with engine.begin() as conn:
        for o in SEED_ORGS:
            conn.execute(organizations.insert().values(
                name=o["name"],
                org_id=o["org_id"],
                scheduler_id=o["scheduler_id"],
                cost_type_id=o["cost_type_id"],
                timezone="America/Los_Angeles",
                resident_days_out=7,
                nonresident_days_out=7,
                release_hour=12,
                release_minute=0,
            ))


def get_conn():
    return engine.connect()


# ── helpers ──────────────────────────────────────────────────────────────────

def row_to_dict(row) -> dict:
    return dict(row._mapping)


def json_field(val) -> list:
    if isinstance(val, list):
        return val
    try:
        return json.loads(val or "[]")
    except Exception:
        return []


def get_days_out(account_id: int, org: dict) -> int:
    """Return days_out for account+org based on residency (defaults to nonresident)."""
    with engine.connect() as conn:
        link = conn.execute(
            account_orgs.select().where(
                (account_orgs.c.account_id == account_id) &
                (account_orgs.c.org_id == org["id"])
            )
        ).fetchone()
    if link and link.is_resident:
        return org.get("resident_days_out", 7)
    return org.get("nonresident_days_out", 7)
