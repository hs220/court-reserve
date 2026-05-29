"""APScheduler configuration with SQLite job store for persistence across restarts."""

import json
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.triggers.date import DateTrigger
from sqlalchemy import update, text

from web.database import DB_PATH, engine, jobs

scheduler = BackgroundScheduler(
    jobstores={"default": SQLAlchemyJobStore(
        url=f"sqlite:///{DB_PATH}",
        engine_options={"connect_args": {"timeout": 30}},
    )},
    job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": None},
)


def start():
    if not scheduler.running:
        scheduler.start()


def shutdown():
    if scheduler.running:
        scheduler.shutdown(wait=False)


# ── book_next ─────────────────────────────────────────────────────────────────

def schedule_book_next(job_id: int, account_id: int, params: dict):
    """Schedule a one-shot book_next job. Fires at params['run_at'] if provided, else immediately."""
    from web.job_runner import run_book_next
    apscheduler_id = f"book_next_{job_id}"
    run_at = params.get("run_at")
    trigger = DateTrigger(run_date=datetime.fromisoformat(run_at)) if run_at else DateTrigger(run_date=datetime.now())

    scheduler.add_job(
        run_book_next,
        trigger,
        id=apscheduler_id,
        kwargs={
            "job_id": job_id,
            "account_id": account_id,
            "target_date_iso": params.get("date"),
            "target_time": params.get("time", ""),
            "duration_override": int(params.get("duration") or 0),
        },
        replace_existing=True,
    )
    _persist_scheduler_id(job_id, apscheduler_id)
    return apscheduler_id


# ── watch ─────────────────────────────────────────────────────────────────────

def schedule_watch(job_id: int, account_id: int, params: dict):
    """Schedule a one-shot watch job to run immediately (or at run_at if provided)."""
    from web.job_runner import run_watch
    apscheduler_id = f"watch_{job_id}"
    run_at = params.get("run_at")
    trigger = DateTrigger(run_date=datetime.fromisoformat(run_at)) if run_at else DateTrigger(run_date=datetime.now())

    probe_id = params.get("probe_account_id")
    scheduler.add_job(
        run_watch,
        trigger,
        id=apscheduler_id,
        kwargs={
            "job_id": job_id,
            "account_id": account_id,
            "target_date_iso": params["date"],
            "target_time": params["time"],
            "duration": int(params.get("duration", 120)),
            "interval": int(params.get("interval", 60)),
            "timeout_minutes": int(params.get("timeout", 0)),
            "probe_account_id": int(probe_id) if probe_id else None,
        },
        replace_existing=True,
    )
    _persist_scheduler_id(job_id, apscheduler_id)
    return apscheduler_id


# ── management ────────────────────────────────────────────────────────────────

def pause_job(apscheduler_id: str):
    try:
        scheduler.pause_job(apscheduler_id)
    except Exception:
        pass


def resume_job(apscheduler_id: str):
    try:
        scheduler.resume_job(apscheduler_id)
    except Exception:
        pass


def remove_job(apscheduler_id: str):
    try:
        scheduler.remove_job(apscheduler_id)
    except Exception:
        pass


def get_next_run(apscheduler_id: str) -> datetime | None:
    try:
        job = scheduler.get_job(apscheduler_id)
        return job.next_run_time if job else None
    except Exception:
        return None


def reschedule_active_watch_jobs():
    """On startup, re-fire any active watch jobs that lost their APScheduler entry."""
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, account_id, params, apscheduler_id FROM jobs "
            "WHERE type='watch' AND status='active'"
        )).fetchall()
    for row in rows:
        job_id, account_id, params_str, aps_id = row
        if not scheduler.get_job(aps_id or ""):
            params = json.loads(params_str or "{}")
            schedule_watch(job_id, account_id, params)


def reschedule_active_book_next_jobs():
    """On startup, re-fire any active book_next jobs that lost their APScheduler entry."""
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, account_id, params, apscheduler_id FROM jobs "
            "WHERE type='book_next' AND status='active'"
        )).fetchall()
    for row in rows:
        job_id, account_id, params_str, aps_id = row
        if not scheduler.get_job(aps_id or ""):
            params = json.loads(params_str or "{}")
            schedule_book_next(job_id, account_id, params)


def _persist_scheduler_id(job_id: int, apscheduler_id: str):
    with engine.begin() as conn:
        conn.execute(
            update(jobs).where(jobs.c.id == job_id).values(apscheduler_id=apscheduler_id)
        )
