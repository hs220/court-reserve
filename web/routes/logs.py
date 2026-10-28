from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from starlette.concurrency import run_in_threadpool

from log_scrub import scrub as scrub_log
from web.database import engine, job_runs, jobs, accounts, organizations, row_to_dict
from web.templates_shared import templates

router = APIRouter()


@router.get("/runs/{run_id}/log", response_class=PlainTextResponse)
async def get_log(run_id: int):
    with engine.connect() as conn:
        run = conn.execute(job_runs.select().where(job_runs.c.id == run_id)).fetchone()
    if not run:
        return PlainTextResponse("Run not found.", status_code=404)
    # Stored logs are scrubbed when written; this covers rows written before that. Off
    # the event loop, so a huge log can't hold up every other request.
    return PlainTextResponse(await run_in_threadpool(scrub_log, run.log_text or "")
                             or "(no output yet)")


@router.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_detail(request: Request, run_id: int):
    with engine.connect() as conn:
        run = row_to_dict(conn.execute(job_runs.select().where(job_runs.c.id == run_id)).fetchone())
        j = row_to_dict(conn.execute(jobs.select().where(jobs.c.id == run["job_id"])).fetchone())
        acc = row_to_dict(conn.execute(accounts.select().where(accounts.c.id == j["account_id"])).fetchone())
        org = row_to_dict(conn.execute(organizations.select().where(organizations.c.id == j["org_id"])).fetchone())
    run["log_text"] = await run_in_threadpool(scrub_log, run["log_text"] or "")
    return templates.TemplateResponse(request, "run_detail.html", context={
        "run": run,
        "job": j,
        "account": acc,
        "org": org,
    })
