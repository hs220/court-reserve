import json
from datetime import date as date_cls, datetime, timedelta

import pytz
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select, update

from web.database import engine, jobs, accounts, organizations, job_runs, bookings, row_to_dict, json_field, get_days_out
from web import apscheduler_setup
from web.templates_shared import templates

router = APIRouter()


def _watch_window_run_at(account_id: int, org: dict, date_str: str) -> str:
    """Return ISO datetime string when the booking window opens for this date, or '' if already open."""
    tz = pytz.timezone(org["timezone"])
    days_out = get_days_out(account_id, org)
    window_open_date = date_cls.fromisoformat(date_str) - timedelta(days=days_out)
    window_open_dt = tz.localize(datetime(
        window_open_date.year, window_open_date.month, window_open_date.day,
        org["release_hour"], org["release_minute"], 0,
    ))
    if window_open_dt > datetime.now(tz):
        return window_open_dt.strftime("%Y-%m-%dT%H:%M")
    return ""


def _recurrent_cron_expr(account_id: int, org: dict, target_dow: int, offset_minutes: int = -2) -> str:
    """Return a cron string for display/reference."""
    days_out = get_days_out(account_id, org)
    fire_dow, fire_hour, fire_minute = apscheduler_setup._recurrent_fire_params(
        target_dow, days_out, org.get("release_hour", 12), org.get("release_minute", 0),
        offset_minutes=offset_minutes,
    )
    return f"{fire_minute} {fire_hour} * * {fire_dow}"


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
        days_out = get_days_out(account_id, org)
        fire_date = date_cls.fromisoformat(date) - timedelta(days=days_out)
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
    probe_account_id: str = Form(""),
):
    probe_id = int(probe_account_id) if probe_account_id.strip() else None

    with engine.connect() as conn:
        org = row_to_dict(conn.execute(organizations.select().where(organizations.c.id == org_db_id)).fetchone())

    run_at = _watch_window_run_at(account_id, org, date)

    params = json.dumps({"date": date, "time": time, "duration": duration,
                         "interval": interval, "timeout": timeout,
                         "probe_account_id": probe_id, "run_at": run_at})
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
                                      "interval": interval, "timeout": timeout,
                                      "probe_account_id": probe_id, "run_at": run_at})
    return RedirectResponse("/jobs", status_code=303)


@router.post("/jobs/recurrent_book_next")
async def create_recurrent_book_next(
    account_id: int = Form(...),
    org_db_id: int = Form(...),
    target_day_of_week: int = Form(...),
    time: str = Form(""),
    duration: int = Form(0),
):
    with engine.connect() as conn:
        org = row_to_dict(conn.execute(organizations.select().where(organizations.c.id == org_db_id)).fetchone())

    params_dict = {"target_day_of_week": target_day_of_week, "time": time, "duration": duration}
    cron_expr = _recurrent_cron_expr(account_id, org, target_day_of_week)

    with engine.begin() as conn:
        result = conn.execute(jobs.insert().values(
            account_id=account_id,
            org_id=org_db_id,
            type="recurrent_book_next",
            params=json.dumps(params_dict),
            status="active",
            cron_expr=cron_expr,
        ))
        job_id = result.inserted_primary_key[0]

    apscheduler_setup.schedule_recurrent_book_next(job_id, account_id, params_dict, org)
    return RedirectResponse("/jobs", status_code=303)


@router.post("/jobs/recurrent_watch")
async def create_recurrent_watch(
    account_id: int = Form(...),
    org_db_id: int = Form(...),
    target_day_of_week: int = Form(...),
    time: str = Form(""),
    duration: int = Form(120),
    interval: int = Form(60),
    timeout: int = Form(0),
    probe_account_id: str = Form(""),
):
    probe_id = int(probe_account_id) if probe_account_id.strip() else None

    with engine.connect() as conn:
        org = row_to_dict(conn.execute(organizations.select().where(organizations.c.id == org_db_id)).fetchone())

    params_dict = {"target_day_of_week": target_day_of_week, "time": time, "duration": duration,
                   "interval": interval, "timeout": timeout, "probe_account_id": probe_id}
    cron_expr = _recurrent_cron_expr(account_id, org, target_day_of_week, offset_minutes=5)

    with engine.begin() as conn:
        result = conn.execute(jobs.insert().values(
            account_id=account_id,
            org_id=org_db_id,
            type="recurrent_watch",
            params=json.dumps(params_dict),
            status="active",
            cron_expr=cron_expr,
        ))
        job_id = result.inserted_primary_key[0]

    apscheduler_setup.schedule_recurrent_watch(job_id, account_id, params_dict, org)
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
        org = row_to_dict(conn.execute(organizations.select().where(organizations.c.id == j["org_id"])).fetchone())
    params = json.loads(j.get("params") or "{}")
    with engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(status="active"))
    if j["type"] == "book_next":
        apscheduler_setup.schedule_book_next(job_id, j["account_id"], params)
    elif j["type"] == "recurrent_book_next":
        apscheduler_setup.schedule_recurrent_book_next(job_id, j["account_id"], params, org)
    elif j["type"] == "recurrent_watch":
        apscheduler_setup.schedule_recurrent_watch(job_id, j["account_id"], params, org)
    else:
        apscheduler_setup.schedule_watch(job_id, j["account_id"], params)
    return RedirectResponse("/jobs", status_code=303)


@router.post("/jobs/{job_id}/run_now")
async def run_now(job_id: int):
    """Trigger a book_next (or recurrent_book_next) job immediately, skipping any wait."""
    import threading
    from web.job_runner import run_book_next
    with engine.connect() as conn:
        j = row_to_dict(conn.execute(jobs.select().where(jobs.c.id == job_id)).fetchone())
    params = json.loads(j.get("params") or "{}")
    is_recurrent = j["type"] == "recurrent_book_next"
    t = threading.Thread(target=run_book_next, kwargs={
        "job_id": job_id,
        "account_id": j["account_id"],
        "at_iso": datetime.now().isoformat(),
        "target_date_iso": params.get("date"),  # None for recurrent — computes from days_out
        "target_time": params.get("time", ""),
        "duration_override": int(params.get("duration") or 0),
        "is_recurrent": is_recurrent,
    }, daemon=True)
    t.start()
    return RedirectResponse(f"/jobs/{job_id}/runs", status_code=303)


@router.get("/jobs/{job_id}/edit", response_class=HTMLResponse)
async def edit_job_form(request: Request, job_id: int):
    with engine.connect() as conn:
        j = _enrich_job(row_to_dict(conn.execute(jobs.select().where(jobs.c.id == job_id)).fetchone()), conn)
        all_accounts = [row_to_dict(r) for r in conn.execute(accounts.select())]
    return templates.TemplateResponse(request, "job_edit.html", context={"job": j, "accounts": all_accounts})


@router.post("/jobs/{job_id}/edit")
async def update_job(
    job_id: int,
    date: str = Form(""),
    target_day_of_week: str = Form(""),
    time: str = Form(""),
    duration: int = Form(0),
    run_at: str = Form(""),
    interval: int = Form(60),
    timeout: int = Form(0),
    probe_account_id: str = Form(""),
):
    with engine.connect() as conn:
        j = row_to_dict(conn.execute(jobs.select().where(jobs.c.id == job_id)).fetchone())
        org = row_to_dict(conn.execute(organizations.select().where(organizations.c.id == j["org_id"])).fetchone())

    apscheduler_setup.remove_job(j.get("apscheduler_id", ""))

    if j["type"] == "book_next":
        if not run_at:
            tz = pytz.timezone(org["timezone"])
            days_out = get_days_out(j["account_id"], org)
            fire_date = date_cls.fromisoformat(date) - timedelta(days=days_out)
            fire_dt = tz.localize(datetime(
                fire_date.year, fire_date.month, fire_date.day,
                org["release_hour"], org["release_minute"], 0,
            )) - timedelta(minutes=2)
            if fire_dt > datetime.now(tz):
                run_at = fire_dt.strftime("%Y-%m-%dT%H:%M")
        new_params = json.dumps({"date": date, "time": time, "duration": duration, "run_at": run_at})
        with engine.begin() as conn:
            conn.execute(update(jobs).where(jobs.c.id == job_id).values(params=new_params, status="active"))
        apscheduler_setup.schedule_book_next(job_id, j["account_id"], json.loads(new_params))

    elif j["type"] == "watch":
        probe_id = int(probe_account_id) if probe_account_id.strip() else None
        run_at = _watch_window_run_at(j["account_id"], org, date)
        new_params = json.dumps({"date": date, "time": time, "duration": duration,
                                  "interval": interval, "timeout": timeout,
                                  "probe_account_id": probe_id, "run_at": run_at})
        with engine.begin() as conn:
            conn.execute(update(jobs).where(jobs.c.id == job_id).values(params=new_params, status="active"))
        apscheduler_setup.schedule_watch(job_id, j["account_id"], json.loads(new_params))

    elif j["type"] in ("recurrent_book_next", "recurrent_watch"):
        dow = int(target_day_of_week) if target_day_of_week.strip() else 0
        cron_expr = _recurrent_cron_expr(j["account_id"], org, dow)
        if j["type"] == "recurrent_book_next":
            new_params = json.dumps({"target_day_of_week": dow, "time": time, "duration": duration})
            with engine.begin() as conn:
                conn.execute(update(jobs).where(jobs.c.id == job_id).values(
                    params=new_params, cron_expr=cron_expr, status="active"))
            apscheduler_setup.schedule_recurrent_book_next(job_id, j["account_id"], json.loads(new_params), org)
        else:
            probe_id = int(probe_account_id) if probe_account_id.strip() else None
            new_params = json.dumps({"target_day_of_week": dow, "time": time, "duration": duration,
                                      "interval": interval, "timeout": timeout, "probe_account_id": probe_id})
            cron_expr = _recurrent_cron_expr(j["account_id"], org, dow, offset_minutes=5)
            with engine.begin() as conn:
                conn.execute(update(jobs).where(jobs.c.id == job_id).values(
                    params=new_params, cron_expr=cron_expr, status="active"))
            apscheduler_setup.schedule_recurrent_watch(job_id, j["account_id"], json.loads(new_params), org)

    return RedirectResponse("/jobs", status_code=303)


@router.get("/jobs/{job_id}/runs", response_class=HTMLResponse)
async def job_runs_page(request: Request, job_id: int):
    with engine.connect() as conn:
        j = _enrich_job(row_to_dict(conn.execute(jobs.select().where(jobs.c.id == job_id)).fetchone()), conn)
        runs = [row_to_dict(r) for r in conn.execute(
            job_runs.select().where(job_runs.c.job_id == job_id).order_by(job_runs.c.started_at.desc())
        )]
    return templates.TemplateResponse(request, "job_detail.html", context={"job": j, "runs": runs})
