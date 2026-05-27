import json
from pathlib import Path

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from sqlalchemy import text
from web.database import engine, organizations, accounts, row_to_dict, json_field, DATA_DIR
from web.templates_shared import templates

router = APIRouter()


def _verify_login(email: str, password: str, org_url: str, session_file: Path) -> tuple[bool, str]:
    """Always does a fresh credential login to confirm email+password are correct."""
    from playwright.sync_api import sync_playwright
    from auth import login, save_session, BROWSER_ARGS, USER_AGENT
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=BROWSER_ARGS)
            ctx = browser.new_context(user_agent=USER_AGENT)
            page = ctx.new_page()
            login(page, email, password, org_url=org_url)  # raises RuntimeError on bad creds
            save_session(ctx, session_file)
            browser.close()
        return True, "Login successful."
    except RuntimeError as e:
        return False, str(e)
    except Exception as e:
        return False, f"Unexpected error: {e}"


def _last_check_time(log_text: str) -> str | None:
    """Extract the most recent [YYYY-MM-DD HH:MM:SS] timestamp from a watch log."""
    import re
    matches = re.findall(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]', log_text or "")
    return matches[-1] if matches else None


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    with engine.connect() as conn:
        orgs = [row_to_dict(r) for r in conn.execute(organizations.select())]

        # All watch and book_next jobs with their latest run
        all_jobs = conn.execute(text("""
            SELECT j.id, j.type, j.params, j.status,
                   o.name as org_name,
                   COALESCE(a.label, a.email) as account_label,
                   jr.id as run_id, jr.status as run_status,
                   jr.started_at, jr.log_text
            FROM jobs j
            JOIN organizations o ON o.id = j.org_id
            JOIN accounts a ON a.id = j.account_id
            LEFT JOIN job_runs jr ON jr.id = (
                SELECT id FROM job_runs WHERE job_id = j.id ORDER BY id DESC LIMIT 1
            )
            WHERE j.type IN ('watch', 'book_next')
            ORDER BY j.id DESC
        """)).fetchall()

    active_jobs = []
    for r in all_jobs:
        try:
            params = json.loads(r[2] or "{}")
        except Exception:
            params = {}
        active_jobs.append({
            "job_id": r[0],
            "job_type": r[1],
            "params": params,
            "job_status": r[3],
            "org_name": r[4],
            "account_label": r[5],
            "run_id": r[6],
            "run_status": r[7],
            "started_at": r[8],
            "last_check": _last_check_time(r[9]) if r[1] == "watch" else None,
        })

    return templates.TemplateResponse(request, "dashboard.html", context={
        "orgs": orgs,
        "active_jobs": active_jobs,
    })


@router.get("/orgs", response_class=HTMLResponse)
async def list_orgs(request: Request):
    with engine.connect() as conn:
        orgs = [row_to_dict(r) for r in conn.execute(organizations.select())]
        for o in orgs:
            o["preferred_times"] = json_field(o.get("preferred_times"))
            o["preferred_courts"] = json_field(o.get("preferred_courts"))
            accs = conn.execute(accounts.select().where(accounts.c.org_id == o["id"])).fetchall()
            o["accounts"] = [row_to_dict(a) for a in accs]
    return templates.TemplateResponse(request, "orgs.html", context={"orgs": orgs})


@router.post("/orgs", response_class=HTMLResponse)
async def create_org(
    request: Request,
    name: str = Form(...),
    org_id: str = Form(...),
    scheduler_id: str = Form(...),
    cost_type_id: str = Form(...),
    timezone: str = Form("America/Los_Angeles"),
    preferred_times: str = Form(""),
    preferred_courts: str = Form(""),
    default_duration: int = Form(120),
    days_out: int = Form(7),
    release_hour: int = Form(12),
    release_minute: int = Form(0),
):
    pt = json.dumps([t.strip() for t in preferred_times.split(",") if t.strip()])
    pc = json.dumps([c.strip() for c in preferred_courts.split(",") if c.strip()])
    with engine.begin() as conn:
        conn.execute(organizations.insert().values(
            name=name, org_id=org_id, scheduler_id=scheduler_id, cost_type_id=cost_type_id,
            timezone=timezone, preferred_times=pt, preferred_courts=pc,
            default_duration=default_duration, days_out=days_out,
            release_hour=release_hour, release_minute=release_minute,
        ))
    return RedirectResponse("/orgs", status_code=303)


@router.get("/orgs/{org_db_id}/edit", response_class=HTMLResponse)
async def edit_org_form(request: Request, org_db_id: int):
    with engine.connect() as conn:
        org = row_to_dict(conn.execute(
            organizations.select().where(organizations.c.id == org_db_id)
        ).fetchone())
    org["preferred_times"] = ", ".join(json_field(org.get("preferred_times")))
    org["preferred_courts"] = ", ".join(json_field(org.get("preferred_courts")))
    return templates.TemplateResponse(request, "org_edit.html", context={"org": org})


@router.post("/orgs/{org_db_id}/edit", response_class=HTMLResponse)
async def update_org(
    request: Request,
    org_db_id: int,
    name: str = Form(...),
    org_id: str = Form(...),
    scheduler_id: str = Form(...),
    cost_type_id: str = Form(...),
    timezone: str = Form("America/Los_Angeles"),
    preferred_times: str = Form(""),
    preferred_courts: str = Form(""),
    default_duration: int = Form(120),
    days_out: int = Form(7),
    release_hour: int = Form(12),
    release_minute: int = Form(0),
):
    pt = json.dumps([t.strip() for t in preferred_times.split(",") if t.strip()])
    pc = json.dumps([c.strip() for c in preferred_courts.split(",") if c.strip()])
    with engine.begin() as conn:
        conn.execute(
            organizations.update().where(organizations.c.id == org_db_id).values(
                name=name, org_id=org_id, scheduler_id=scheduler_id, cost_type_id=cost_type_id,
                timezone=timezone, preferred_times=pt, preferred_courts=pc,
                default_duration=default_duration, days_out=days_out,
                release_hour=release_hour, release_minute=release_minute,
            )
        )
    return RedirectResponse("/orgs", status_code=303)


@router.post("/orgs/{org_db_id}/delete")
async def delete_org(org_db_id: int):
    with engine.begin() as conn:
        conn.execute(accounts.delete().where(accounts.c.org_id == org_db_id))
        conn.execute(organizations.delete().where(organizations.c.id == org_db_id))
    return RedirectResponse("/orgs", status_code=303)


# ── Accounts ──────────────────────────────────────────────────────────────────

@router.post("/orgs/{org_db_id}/accounts", response_class=HTMLResponse)
async def create_account(
    request: Request,
    org_db_id: int,
    label: str = Form(""),
    email: str = Form(...),
    password: str = Form(...),
):
    with engine.connect() as conn:
        org = row_to_dict(conn.execute(
            organizations.select().where(organizations.c.id == org_db_id)
        ).fetchone())

    session_file = DATA_DIR / f"session_{email.replace('@', '_').replace('.', '_')}.json"
    org_url = f"https://app.courtreserve.com/Online/Reservations/Bookings/{org['org_id']}?sId={org['scheduler_id']}"

    ok, msg = await run_in_threadpool(_verify_login, email, password, org_url, session_file)

    if not ok:
        # Re-render the full orgs page with the error surfaced next to this org
        with engine.connect() as conn:
            orgs = [row_to_dict(r) for r in conn.execute(organizations.select())]
            for o in orgs:
                o["preferred_times"] = json_field(o.get("preferred_times"))
                o["preferred_courts"] = json_field(o.get("preferred_courts"))
                accs = conn.execute(accounts.select().where(accounts.c.org_id == o["id"])).fetchall()
                o["accounts"] = [row_to_dict(a) for a in accs]
        return templates.TemplateResponse(request, "orgs.html", context={
            "orgs": orgs,
            "account_error": {"org_id": org_db_id, "message": msg},
        }, status_code=200)

    with engine.begin() as conn:
        conn.execute(accounts.insert().values(
            org_id=org_db_id, label=label, email=email,
            password=password, session_file=str(session_file),
        ))
    return RedirectResponse("/orgs", status_code=303)


@router.post("/accounts/{account_id}/delete")
async def delete_account(account_id: int):
    with engine.begin() as conn:
        conn.execute(accounts.delete().where(accounts.c.id == account_id))
    return RedirectResponse("/orgs", status_code=303)
