import json
from datetime import date as date_cls, datetime, timedelta

import pytz
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select, update

from web.database import engine, jobs, accounts, organizations, job_runs, bookings, row_to_dict, json_field
from web import apscheduler_setup
from web.templates_shared import templates

router = APIRouter()


def _enrich_job(j: dict, conn) -> dict:
    acc = conn.execute(accounts.select().where(accounts.c.id == j["account_id"])).fetchone()
    org = conn.execute(organizations.select().where(organizations.c.id == j["org_id"])).fetchone()
    j["account"] = row_to_dict(acc) if acc else {}
    j["org"] = row_to_dict(org) if org else {}
    j["params"] = json.loads(j.get("params") or "{}")
    next_run = apscheduler_setup.get_next_run(j.get("apscheduler_id", ""))
    j["next_run_at"] = next_run
    return j


@router.get("/jobs", response_class=HTMLResponse)
async def list_jobs(request: Request):
    with engine.connect() as conn:
        all_jobs = [_enrich_job(row_to_dict(r), conn)
                    for r in conn.execute(jobs.select().order_by(jobs.c.created_at.desc()))]
        all_orgs = [row_to_dict(r) for r in conn.execute(organizations.select())]
        all_accounts = [row_to_dict(r) for r in conn.execute(accounts.select())]
    return templates.TemplateResponse(request, "jobs.html", context={
        "jobs": all_jobs,
        "orgs": all_orgs,
        "accounts": all_accounts,
    })


@router.post("/jobs/book_next")
async def create_book_next(
    account_id: int = Form(...),
    org_db_id: int = Form(...),
    date: str = Form(...),
    time: str = Form(""),
    duration: int = Form(0),
    run_at: str = Form(""),
):
    with engine.connect() as conn:
        org = row_to_dict(conn.execute(organizations.select().where(organizations.c.id == org_db_id)).fetchone())

    if not run_at:
        tz = pytz.timezone(org["timezone"])
        fire_date = date_cls.fromisoformat(date) - timedelta(days=org["days_out"])
        fire_dt = tz.localize(datetime(
            fire_date.year, fire_date.month, fire_date.day,
            org["release_hour"], org["release_minute"], 0,
        )) - timedelta(minutes=2)
        if fire_dt > datetime.now(tz):
            run_at = fire_dt.strftime("%Y-%m-%dT%H:%M")

    params = json.dumps({"date": date, "time": time, "duration": duration, "run_at": run_at})
    with engine.begin() as conn:
        result = conn.execute(jobs.insert().values(
            account_id=account_id,
            org_id=org_db_id,
            type="book_next",
            params=params,
            status="active",
            cron_expr="",
        ))
        job_id = result.inserted_primary_key[0]

    apscheduler_setup.schedule_book_next(job_id, account_id,
                                         {"date": date, "time": time, "duration": duration, "run_at": run_at})
    return RedirectResponse("/jobs", status_code=303)


@router.post("/jobs/watch")
async def create_watch(
    account_id: int = Form(...),
    org_db_id: int = Form(...),
    date: str = Form(...),
    time: str = Form(""),
    duration: int = Form(120),
    interval: int = Form(60),
    timeout: int = Form(0),
):
    params = json.dumps({"date": date, "time": time, "duration": duration,
                         "interval": interval, "timeout": timeout})
    with engine.begin() as conn:
        result = conn.execute(jobs.insert().values(
            account_id=account_id,
            org_id=org_db_id,
            type="watch",
            params=params,
            status="active",
            cron_expr="",
        ))
        job_id = result.inserted_primary_key[0]

    apscheduler_setup.schedule_watch(job_id, account_id,
                                     {"date": date, "time": time, "duration": duration,
                                      "interval": interval, "timeout": timeout})
    return RedirectResponse("/jobs", status_code=303)


@router.post("/jobs/{job_id}/pause")
async def pause_job(job_id: int):
    with engine.connect() as conn:
        j = row_to_dict(conn.execute(jobs.select().where(jobs.c.id == job_id)).fetchone())
    apscheduler_setup.pause_job(j.get("apscheduler_id", ""))
    with engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(status="paused"))
    return RedirectResponse("/jobs", status_code=303)


@router.post("/jobs/{job_id}/resume")
async def resume_job(job_id: int):
    with engine.connect() as conn:
        j = row_to_dict(conn.execute(jobs.select().where(jobs.c.id == job_id)).fetchone())
    apscheduler_setup.resume_job(j.get("apscheduler_id", ""))
    with engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(status="active"))
    return RedirectResponse("/jobs", status_code=303)


@router.post("/jobs/{job_id}/delete")
async def delete_job(job_id: int):
    with engine.connect() as conn:
        j = row_to_dict(conn.execute(jobs.select().where(jobs.c.id == job_id)).fetchone())
        run_ids = [r[0] for r in conn.execute(
            job_runs.select().where(job_runs.c.job_id == job_id)
        ).fetchall()]
    apscheduler_setup.remove_job(j.get("apscheduler_id", ""))
    with engine.begin() as conn:
        if run_ids:
            conn.execute(bookings.delete().where(bookings.c.job_run_id.in_(run_ids)))
        conn.execute(job_runs.delete().where(job_runs.c.job_id == job_id))
        conn.execute(jobs.delete().where(jobs.c.id == job_id))
    return RedirectResponse("/jobs", status_code=303)


@router.post("/jobs/{job_id}/restart")
async def restart_job(job_id: int):
    """Re-schedule a failed/completed job with the same parameters."""
    with engine.connect() as conn:
        j = row_to_dict(conn.execute(jobs.select().where(jobs.c.id == job_id)).fetchone())
    params = json.loads(j.get("params") or "{}")
    with engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(status="active"))
    if j["type"] == "book_next":
        apscheduler_setup.schedule_book_next(job_id, j["account_id"], params)
    else:
        apscheduler_setup.schedule_watch(job_id, j["account_id"], params)
    return RedirectResponse("/jobs", status_code=303)


@router.post("/jobs/{job_id}/run_now")
async def run_now(job_id: int):
    """Trigger a book_next job immediately, skipping any scheduled wait."""
    import threading
    from web.job_runner import run_book_next
    with engine.connect() as conn:
        j = row_to_dict(conn.execute(jobs.select().where(jobs.c.id == job_id)).fetchone())
    params = json.loads(j.get("params") or "{}")
    t = threading.Thread(target=run_book_next, kwargs={
        "job_id": job_id,
        "account_id": j["account_id"],
        "at_iso": datetime.now().isoformat(),  # ensures wait_until returns immediately
        "target_date_iso": params.get("date"),
        "target_time": params.get("time", ""),
        "duration_override": int(params.get("duration") or 0),
    }, daemon=True)
    t.start()
    return RedirectResponse(f"/jobs/{job_id}/runs", status_code=303)


@router.get("/jobs/{job_id}/edit", response_class=HTMLResponse)
async def edit_job_form(request: Request, job_id: int):
    with engine.connect() as conn:
        j = _enrich_job(row_to_dict(conn.execute(jobs.select().where(jobs.c.id == job_id)).fetchone()), conn)
    return templates.TemplateResponse(request, "job_edit.html", context={"job": j})


@router.post("/jobs/{job_id}/edit")
async def update_job(
    job_id: int,
    date: str = Form(...),
    time: str = Form(""),
    duration: int = Form(0),
    run_at: str = Form(""),
    interval: int = Form(60),
    timeout: int = Form(0),
):
    with engine.connect() as conn:
        j = row_to_dict(conn.execute(jobs.select().where(jobs.c.id == job_id)).fetchone())
        org = row_to_dict(conn.execute(organizations.select().where(organizations.c.id == j["org_id"])).fetchone())

    if j["type"] == "book_next":
        if not run_at:
            tz = pytz.timezone(org["timezone"])
            fire_date = date_cls.fromisoformat(date) - timedelta(days=org["days_out"])
            fire_dt = tz.localize(datetime(
                fire_date.year, fire_date.month, fire_date.day,
                org["release_hour"], org["release_minute"], 0,
            )) - timedelta(minutes=2)
            if fire_dt > datetime.now(tz):
                run_at = fire_dt.strftime("%Y-%m-%dT%H:%M")
        new_params = json.dumps({"date": date, "time": time, "duration": duration, "run_at": run_at})
    else:
        new_params = json.dumps({"date": date, "time": time, "duration": duration,
                                  "interval": interval, "timeout": timeout})

    apscheduler_setup.remove_job(j.get("apscheduler_id", ""))
    with engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(params=new_params, status="active"))

    params_dict = json.loads(new_params)
    if j["type"] == "book_next":
        apscheduler_setup.schedule_book_next(job_id, j["account_id"], params_dict)
    else:
        apscheduler_setup.schedule_watch(job_id, j["account_id"], params_dict)

    return RedirectResponse("/jobs", status_code=303)


@router.get("/jobs/{job_id}/runs", response_class=HTMLResponse)
async def job_runs_page(request: Request, job_id: int):
    with engine.connect() as conn:
        j = _enrich_job(row_to_dict(conn.execute(jobs.select().where(jobs.c.id == job_id)).fetchone()), conn)
        runs = [row_to_dict(r) for r in conn.execute(
            job_runs.select().where(job_runs.c.job_id == job_id).order_by(job_runs.c.started_at.desc())
        )]
    return templates.TemplateResponse(request, "job_detail.html", context={"job": j, "runs": runs})
