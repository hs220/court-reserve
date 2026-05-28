import sys
from datetime import datetime
from pathlib import Path

# Ensure the project root is on sys.path so `booking`, `auth`, etc. are importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from sqlalchemy import update, func

from web.database import init_db, engine, job_runs
from web.database import jobs as jobs_table
from web import apscheduler_setup
from web.routes import orgs, jobs, logs, calendar as calendar_routes, reservations

STATIC_DIR = Path(__file__).parent / "static"


def _cleanup_orphaned_runs():
    """Mark any runs still in 'running' state as failed — they were killed by a previous restart."""
    from sqlalchemy import text, select
    with engine.begin() as conn:
        # Find affected watch jobs before marking runs (need job type to decide job status)
        affected = conn.execute(text("""
            SELECT DISTINCT j.id FROM job_runs jr
            JOIN jobs j ON j.id = jr.job_id
            WHERE jr.status = 'running' AND j.type = 'watch'
        """)).fetchall()

        conn.execute(
            update(job_runs)
            .where(job_runs.c.status == "running")
            .values(
                status="failed",
                finished_at=datetime.utcnow(),
                log_text=job_runs.c.log_text + "\n[Run interrupted: server restarted]",
            )
        )

        # (watch job status is managed by run_watch itself and reschedule_active_watch_jobs)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _cleanup_orphaned_runs()
    apscheduler_setup.start()
    apscheduler_setup.reschedule_active_watch_jobs()
    apscheduler_setup.reschedule_active_book_next_jobs()
    yield
    apscheduler_setup.shutdown()


app = FastAPI(title="CourtReserve Manager", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(orgs.router)
app.include_router(jobs.router)
app.include_router(logs.router)
app.include_router(calendar_routes.router)
app.include_router(reservations.router)
