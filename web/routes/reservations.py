from datetime import datetime, date as date_cls
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from sqlalchemy import text

from web.database import engine, organizations, accounts, bookings, row_to_dict
from web.templates_shared import templates

router = APIRouter()


def _do_sync() -> dict:
    """Sync reservations from CourtReserve for all accounts. Returns summary counts."""
    from playwright.sync_api import sync_playwright
    from auth import ensure_logged_in, BROWSER_ARGS, USER_AGENT
    from booking import OrgConfig, get_my_reservations

    with engine.connect() as conn:
        orgs_list = [row_to_dict(r) for r in conn.execute(organizations.select())]
        accs_list = [row_to_dict(r) for r in conn.execute(accounts.select())]

    org_by_id = {o["id"]: o for o in orgs_list}

    added = 0
    updated = 0
    removed = 0
    errors = []

    for acc in accs_list:
        org = org_by_id.get(acc["org_id"])
        if not org:
            continue
        org_cfg = OrgConfig(
            org_id=org["org_id"],
            scheduler_id=org["scheduler_id"],
            cost_type_id=org["cost_type_id"],
            timezone=org.get("timezone", "America/Los_Angeles"),
        )
        session_file = Path(acc["session_file"]) if acc.get("session_file") else None

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, args=BROWSER_ARGS)
                ctx_kwargs = {"user_agent": USER_AGENT}
                if session_file and session_file.exists():
                    ctx_kwargs["storage_state"] = str(session_file)
                ctx = browser.new_context(**ctx_kwargs)
                page = ctx.new_page()
                ensure_logged_in(ctx, page, acc["email"], acc["password"],
                                 org_cfg.booking_url, session_file)
                reservations = get_my_reservations(page, org_cfg)
                browser.close()
        except Exception as e:
            errors.append(f"{acc.get('label') or acc['email']}: {e}")
            continue

        # Upsert returned reservations
        live_keys = set()
        for res in reservations:
            key = (res["date"], res["start_time"])
            live_keys.add(key)
            with engine.connect() as conn:
                existing = conn.execute(text("""
                    SELECT id FROM bookings
                    WHERE account_id = :aid AND date = :d AND start_time = :t
                """), {"aid": acc["id"], "d": res["date"], "t": res["start_time"]}).fetchone()

            if existing:
                with engine.begin() as conn:
                    conn.execute(text("""
                        UPDATE bookings SET court_type = :ct, duration_min = :dur
                        WHERE id = :bid
                    """), {"ct": res["court_type"], "dur": res["duration_min"], "bid": existing[0]})
                updated += 1
            else:
                with engine.begin() as conn:
                    conn.execute(bookings.insert().values(
                        job_run_id=None,
                        account_id=acc["id"],
                        date=res["date"],
                        start_time=res["start_time"],
                        court_type=res["court_type"],
                        duration_min=res["duration_min"],
                        confirmed_at=datetime.utcnow(),
                    ))
                added += 1

        # Remove local bookings that are no longer on CourtReserve.
        # Only look at future dates — the API returns ~30 days ahead so anything
        # in that window missing from live_keys has been cancelled.
        today_str = date_cls.today().isoformat()
        with engine.connect() as conn:
            future_rows = conn.execute(text("""
                SELECT id, date, start_time FROM bookings
                WHERE account_id = :aid AND date >= :today
            """), {"aid": acc["id"], "today": today_str}).fetchall()

        stale_ids = [r[0] for r in future_rows if (r[1], r[2]) not in live_keys]
        if stale_ids:
            placeholders = ",".join(f":id{i}" for i in range(len(stale_ids)))
            params = {f"id{i}": v for i, v in enumerate(stale_ids)}
            with engine.begin() as conn:
                conn.execute(text(f"DELETE FROM bookings WHERE id IN ({placeholders})"), params)
            removed += len(stale_ids)

    return {"added": added, "updated": updated, "removed": removed, "errors": errors}


@router.get("/reservations", response_class=HTMLResponse)
async def list_reservations(request: Request):
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT b.id, b.date, b.start_time, b.duration_min, b.court_type, b.confirmed_at,
                   COALESCE(a.label, a.email) as account_label, o.name as org_name
            FROM bookings b
            JOIN accounts a ON a.id = b.account_id
            JOIN organizations o ON o.id = a.org_id
            ORDER BY b.date DESC, b.start_time DESC
        """)).fetchall()

    reservations = [dict(r._mapping) for r in rows]
    return templates.TemplateResponse(request, "reservations.html", context={
        "reservations": reservations,
    })


@router.post("/reservations/sync")
async def sync_reservations(request: Request):
    import urllib.parse
    result = await run_in_threadpool(_do_sync)
    added = result["added"]
    updated = result["updated"]
    removed = result["removed"]
    err_msgs = result["errors"]
    err_param = urllib.parse.quote("; ".join(err_msgs)) if err_msgs else ""
    return RedirectResponse(
        f"/reservations?synced=1&added={added}&updated={updated}&removed={removed}&errmsg={err_param}",
        status_code=303,
    )
