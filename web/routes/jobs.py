import json
from datetime import date as date_cls, datetime, timedelta

import pytz
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from sqlalchemy import select, update

from web.database import (engine, jobs, accounts, organizations, job_runs, bookings,
                          pending_transfers, row_to_dict, json_field, get_days_out)
from web import apscheduler_setup
from web.templates_shared import templates

router = APIRouter()


def _job_list_url(job_type: str) -> str:
    return "/recurring" if job_type.startswith("recurrent_") else "/jobs"


def _watch_window_run_at(account_id: int, org: dict, date_str: str) -> str:
    """Return ISO datetime string 2min before the booking window opens, or '' if already past."""
    tz = pytz.timezone(org["timezone"])
    days_out = get_days_out(account_id, org)
    window_open_date = date_cls.fromisoformat(date_str) - timedelta(days=days_out)
    window_open_dt = tz.localize(datetime(
        window_open_date.year, window_open_date.month, window_open_date.day,
        org["release_hour"], org["release_minute"], 0,
    ))
    fire_dt = window_open_dt - timedelta(minutes=2)
    if fire_dt > datetime.now(tz):
        return fire_dt.strftime("%Y-%m-%dT%H:%M")
    return ""


def _recurrent_cron_expr(account_id: int, org: dict, target_dow: int, job_type: str) -> str:
    """Return a cron string for display/reference."""
    days_out = get_days_out(account_id, org)
    dow = apscheduler_setup.DOW_NAMES
    if job_type == "recurrent_book_next":
        fire_dow = dow[(target_dow - days_out - 1 + 70) % 7]
        return f"0 6 * * {fire_dow}"
    else:  # recurrent_watch
        fire_dow = dow[(target_dow - days_out + 70) % 7]
        total = org.get("release_hour", 12) * 60 + org.get("release_minute", 0) - 2
        return f"{total % 60} {total // 60} * * {fire_dow}"


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
                    for r in conn.execute(
                        jobs.select()
                        .where(jobs.c.type.in_(["book_next", "watch"]))
                        .order_by(jobs.c.created_at.desc())
                    )]
        all_orgs = [row_to_dict(r) for r in conn.execute(organizations.select())]
        all_accounts = [row_to_dict(r) for r in conn.execute(accounts.select())]
        acct_label = {a["id"]: (a.get("label") or a["email"]) for a in all_accounts}
        # Pending-transfers tab: only the non-terminal (still-actionable) transfers —
        # not the failed/expired/transferred ones.
        transfers = [row_to_dict(r) for r in conn.execute(
            pending_transfers.select()
            .where(pending_transfers.c.status.in_(["pending", "transferring", "held_by_probe"]))
            .order_by(pending_transfers.c.created_at.desc())
        )]
        # Map each job to its latest transfer (any status) so jobs can link to it.
        job_transfer = {}
        for r in conn.execute(
            pending_transfers.select().order_by(pending_transfers.c.created_at.asc())
        ):
            job_transfer[r.job_id] = {"id": r.id, "status": r.status}
    for j in all_jobs:
        j["transfer"] = job_transfer.get(j["id"])
    for t in transfers:
        t["probe_label"] = acct_label.get(t["probe_account_id"], "?")
        t["main_label"] = acct_label.get(t["main_account_id"], "?")
    active_jobs = [j for j in all_jobs if j["status"] in ("active", "paused")]
    completed_jobs = [j for j in all_jobs if j["status"] not in ("active", "paused")]
    return templates.TemplateResponse(request, "jobs.html", context={
        "active_jobs": active_jobs,
        "completed_jobs": completed_jobs,
        "pending_transfers": transfers,
        "orgs": all_orgs,
        "accounts": all_accounts,
    })


@router.get("/recurring", response_class=HTMLResponse)
async def list_recurring(request: Request):
    with engine.connect() as conn:
        all_jobs = [_enrich_job(row_to_dict(r), conn)
                    for r in conn.execute(
                        jobs.select()
                        .where(jobs.c.type.in_(["recurrent_book_next", "recurrent_watch"]))
                        .order_by(jobs.c.created_at.desc())
                    )]
        all_orgs = [row_to_dict(r) for r in conn.execute(organizations.select())]
        all_accounts = [row_to_dict(r) for r in conn.execute(accounts.select())]
    return templates.TemplateResponse(request, "recurring.html", context={
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
    deadline_mode: str = Form("4h"),
    probe_account_id: str = Form(""),
    auto_transfer: str = Form(""),
):
    probe_id = int(probe_account_id) if probe_account_id.strip() else None
    # Auto-transfer only makes sense with a distinct probe account.
    auto_xfer = bool(auto_transfer.strip()) and probe_id is not None and probe_id != account_id

    with engine.connect() as conn:
        org = row_to_dict(conn.execute(organizations.select().where(organizations.c.id == org_db_id)).fetchone())

    run_at = _watch_window_run_at(account_id, org, date)

    params = json.dumps({"date": date, "time": time, "duration": duration,
                         "interval": interval, "deadline_mode": deadline_mode,
                         "probe_account_id": probe_id, "auto_transfer": auto_xfer, "run_at": run_at})
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
                                      "interval": interval, "deadline_mode": deadline_mode,
                                      "probe_account_id": probe_id, "auto_transfer": auto_xfer,
                                      "run_at": run_at})
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
    cron_expr = _recurrent_cron_expr(account_id, org, target_day_of_week, "recurrent_book_next")

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
    return RedirectResponse("/recurring", status_code=303)


@router.post("/jobs/recurrent_watch")
async def create_recurrent_watch(
    account_id: int = Form(...),
    org_db_id: int = Form(...),
    target_day_of_week: int = Form(...),
    time: str = Form(""),
    duration: int = Form(120),
    interval: int = Form(60),
    deadline_mode: str = Form("4h"),
    probe_account_id: str = Form(""),
    auto_transfer: str = Form(""),
):
    probe_id = int(probe_account_id) if probe_account_id.strip() else None
    auto_xfer = bool(auto_transfer.strip()) and probe_id is not None and probe_id != account_id

    with engine.connect() as conn:
        org = row_to_dict(conn.execute(organizations.select().where(organizations.c.id == org_db_id)).fetchone())

    params_dict = {"target_day_of_week": target_day_of_week, "time": time, "duration": duration,
                   "interval": interval, "deadline_mode": deadline_mode, "probe_account_id": probe_id,
                   "auto_transfer": auto_xfer}
    cron_expr = _recurrent_cron_expr(account_id, org, target_day_of_week, "recurrent_watch")

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
    return RedirectResponse("/recurring", status_code=303)


@router.post("/jobs/{job_id}/pause")
async def pause_job(job_id: int):
    with engine.connect() as conn:
        j = row_to_dict(conn.execute(jobs.select().where(jobs.c.id == job_id)).fetchone())
    apscheduler_setup.pause_job(j.get("apscheduler_id", ""))
    with engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(status="paused"))
    return RedirectResponse(_job_list_url(j["type"]), status_code=303)


@router.post("/jobs/{job_id}/resume")
async def resume_job(job_id: int):
    with engine.connect() as conn:
        j = row_to_dict(conn.execute(jobs.select().where(jobs.c.id == job_id)).fetchone())
    apscheduler_setup.resume_job(j.get("apscheduler_id", ""))
    with engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(status="active"))
    return RedirectResponse(_job_list_url(j["type"]), status_code=303)


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
    return RedirectResponse(_job_list_url(j["type"]), status_code=303)


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
    return RedirectResponse(_job_list_url(j["type"]), status_code=303)


@router.post("/jobs/{job_id}/run_now")
async def run_now(job_id: int):
    """Trigger a job immediately (book_next, recurrent_book_next, or recurrent_watch)."""
    import threading
    from web.job_runner import run_book_next, run_recurrent_book_next, run_recurrent_watch
    with engine.connect() as conn:
        j = row_to_dict(conn.execute(jobs.select().where(jobs.c.id == job_id)).fetchone())
    params = json.loads(j.get("params") or "{}")

    if j["type"] == "recurrent_book_next":
        t = threading.Thread(target=run_recurrent_book_next,
                             kwargs={"job_id": job_id, "account_id": j["account_id"]}, daemon=True)
    elif j["type"] == "recurrent_watch":
        t = threading.Thread(target=run_recurrent_watch,
                             kwargs={"job_id": job_id, "account_id": j["account_id"]}, daemon=True)
    else:
        t = threading.Thread(target=run_book_next, kwargs={
            "job_id": job_id,
            "account_id": j["account_id"],
            "at_iso": datetime.now().isoformat(),
            "target_date_iso": params.get("date"),
            "target_time": params.get("time", ""),
            "duration_override": int(params.get("duration") or 0),
        }, daemon=True)
    t.start()
    return RedirectResponse(f"/jobs/{job_id}/runs", status_code=303)


@router.post("/transfers/{transfer_id}/execute")
async def execute_transfer(transfer_id: int):
    """Run a pending probe→main transfer in the background (cancel from probe, re-book on main)."""
    import threading
    from web.job_runner import run_transfer
    # Do NOT pre-set status here: run_transfer atomically claims a runnable transfer
    # (pending/held_by_probe -> transferring). Setting 'transferring' here would make that
    # claim never match, so the worker would no-op ("nothing to do") — the original bug.
    with engine.begin() as conn:
        row = conn.execute(pending_transfers.select().where(pending_transfers.c.id == transfer_id)).fetchone()
        if row and row.status in ("pending", "held_by_probe"):
            conn.execute(update(pending_transfers).where(pending_transfers.c.id == transfer_id)
                         .values(note="Transfer queued..."))
    threading.Thread(target=run_transfer, kwargs={"transfer_id": transfer_id}, daemon=True).start()
    return RedirectResponse("/jobs", status_code=303)


@router.get("/transfers/{transfer_id}/log", response_class=PlainTextResponse)
async def get_transfer_log(transfer_id: int):
    with engine.connect() as conn:
        t = conn.execute(pending_transfers.select()
                         .where(pending_transfers.c.id == transfer_id)).fetchone()
    if not t:
        return PlainTextResponse("Transfer not found.", status_code=404)
    return PlainTextResponse(t.log_text or "(no transfer output yet)")


@router.get("/transfers/{transfer_id}", response_class=HTMLResponse)
async def transfer_detail(request: Request, transfer_id: int):
    with engine.connect() as conn:
        row = conn.execute(pending_transfers.select()
                           .where(pending_transfers.c.id == transfer_id)).fetchone()
        if not row:
            return HTMLResponse("Transfer not found.", status_code=404)
        t = row_to_dict(row)
        acct_label = {a.id: (a.label or a.email)
                      for a in conn.execute(accounts.select())}
        org = conn.execute(organizations.select()
                           .where(organizations.c.id == t["org_id"])).fetchone()
    t["probe_label"] = acct_label.get(t["probe_account_id"], "?")
    t["main_label"] = acct_label.get(t["main_account_id"], "?")
    t["org_name"] = org.name if org else "?"
    return templates.TemplateResponse(request, "transfer_detail.html", context={"t": t})


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
    deadline_mode: str = Form("4h"),
    probe_account_id: str = Form(""),
    auto_transfer: str = Form(""),
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
        auto_xfer = bool(auto_transfer.strip()) and probe_id is not None and probe_id != j["account_id"]
        run_at = _watch_window_run_at(j["account_id"], org, date)
        new_params = json.dumps({"date": date, "time": time, "duration": duration,
                                  "interval": interval, "deadline_mode": deadline_mode,
                                  "probe_account_id": probe_id, "auto_transfer": auto_xfer, "run_at": run_at})
        with engine.begin() as conn:
            conn.execute(update(jobs).where(jobs.c.id == job_id).values(params=new_params, status="active"))
        apscheduler_setup.schedule_watch(job_id, j["account_id"], json.loads(new_params))

    elif j["type"] in ("recurrent_book_next", "recurrent_watch"):
        dow = int(target_day_of_week) if target_day_of_week.strip() else 0
        if j["type"] == "recurrent_book_next":
            cron_expr = _recurrent_cron_expr(j["account_id"], org, dow, "recurrent_book_next")
            new_params = json.dumps({"target_day_of_week": dow, "time": time, "duration": duration})
            with engine.begin() as conn:
                conn.execute(update(jobs).where(jobs.c.id == job_id).values(
                    params=new_params, cron_expr=cron_expr, status="active"))
            apscheduler_setup.schedule_recurrent_book_next(job_id, j["account_id"], json.loads(new_params), org)
        else:
            cron_expr = _recurrent_cron_expr(j["account_id"], org, dow, "recurrent_watch")
            probe_id = int(probe_account_id) if probe_account_id.strip() else None
            new_params = json.dumps({"target_day_of_week": dow, "time": time, "duration": duration,
                                      "interval": interval, "deadline_mode": deadline_mode, "probe_account_id": probe_id})
            with engine.begin() as conn:
                conn.execute(update(jobs).where(jobs.c.id == job_id).values(
                    params=new_params, cron_expr=cron_expr, status="active"))
            apscheduler_setup.schedule_recurrent_watch(job_id, j["account_id"], json.loads(new_params), org)

    return RedirectResponse(_job_list_url(j["type"]), status_code=303)


@router.get("/jobs/{job_id}/runs", response_class=HTMLResponse)
async def job_runs_page(request: Request, job_id: int):
    with engine.connect() as conn:
        j = _enrich_job(row_to_dict(conn.execute(jobs.select().where(jobs.c.id == job_id)).fetchone()), conn)
        runs = [row_to_dict(r) for r in conn.execute(
            job_runs.select().where(job_runs.c.job_id == job_id).order_by(job_runs.c.started_at.desc())
        )]
    return templates.TemplateResponse(request, "job_detail.html", context={"job": j, "runs": runs})
