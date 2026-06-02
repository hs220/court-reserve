import urllib.parse
from datetime import datetime

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from sqlalchemy import update

from web import github_issues
from web.database import engine, bug_reports, row_to_dict
from web.templates_shared import templates

router = APIRouter()


def _issue_body(description: str, severity: str, reporter: str) -> str:
    lines = [
        f"**Severity:** {severity}",
        f"**Reported by:** {reporter or '(anonymous)'}",
        "",
        description or "_No description provided._",
        "",
        "---",
        "_Filed automatically from the CourtReserve Manager bug-report form._",
    ]
    return "\n".join(lines)


@router.get("/bugs", response_class=HTMLResponse)
async def list_bugs(request: Request):
    with engine.connect() as conn:
        rows = conn.execute(
            bug_reports.select().order_by(
                bug_reports.c.status.asc(),       # open before resolved
                bug_reports.c.created_at.desc(),
            )
        ).fetchall()
    reports = [row_to_dict(r) for r in rows]
    open_count = sum(1 for r in reports if r["status"] == "open")
    return templates.TemplateResponse(request, "bug_reports.html", context={
        "reports": reports,
        "open_count": open_count,
        "gh_configured": github_issues.is_configured(),
    })


@router.post("/bugs")
async def create_bug(
    title: str = Form(...),
    description: str = Form(""),
    severity: str = Form("medium"),
    reporter: str = Form(""),
):
    title = title.strip()
    if not title:
        return RedirectResponse("/bugs", status_code=303)

    severity = severity if severity in ("low", "medium", "high") else "medium"
    description = description.strip()
    reporter = reporter.strip()

    with engine.begin() as conn:
        result = conn.execute(bug_reports.insert().values(
            title=title,
            description=description,
            severity=severity,
            reporter=reporter,
            status="open",
            created_at=datetime.utcnow(),
        ))
        bug_id = result.inserted_primary_key[0]

    # Best-effort: file a GitHub issue if integration is configured.
    gh_warn = ""
    if github_issues.is_configured():
        try:
            issue = await run_in_threadpool(
                github_issues.create_issue,
                f"[bug] {title}",
                _issue_body(description, severity, reporter),
                ["bug", f"severity:{severity}"],
            )
            with engine.begin() as conn:
                conn.execute(
                    update(bug_reports)
                    .where(bug_reports.c.id == bug_id)
                    .values(gh_issue_number=issue["number"], gh_issue_url=issue["url"])
                )
        except Exception as e:
            gh_warn = str(e)

    qs = "submitted=1"
    if gh_warn:
        qs += "&ghwarn=" + urllib.parse.quote(gh_warn)
    return RedirectResponse(f"/bugs?{qs}", status_code=303)


def _redirect(warn: str = "", extra: str = ""):
    params = [p for p in (extra, ("ghwarn=" + urllib.parse.quote(warn)) if warn else "") if p]
    qs = ("?" + "&".join(params)) if params else ""
    return RedirectResponse(f"/bugs{qs}", status_code=303)


async def _push_issue_state(bug_id: int, state: str) -> str:
    """Best-effort: mirror the local change onto the linked GitHub issue.

    Returns a warning string ('' if nothing to do or it succeeded).
    """
    with engine.connect() as conn:
        row = conn.execute(
            bug_reports.select().where(bug_reports.c.id == bug_id)
        ).fetchone()
    if not row or not row.gh_issue_number or not github_issues.is_configured():
        return ""
    try:
        await run_in_threadpool(github_issues.set_issue_state, row.gh_issue_number, state)
        return ""
    except Exception as e:
        return f"issue #{row.gh_issue_number} not updated on GitHub: {e}"


@router.post("/bugs/{bug_id}/resolve")
async def resolve_bug(bug_id: int):
    with engine.begin() as conn:
        conn.execute(
            update(bug_reports)
            .where(bug_reports.c.id == bug_id)
            .values(status="resolved", resolved_at=datetime.utcnow())
        )
    warn = await _push_issue_state(bug_id, "closed")
    return _redirect(warn)


@router.post("/bugs/{bug_id}/reopen")
async def reopen_bug(bug_id: int):
    with engine.begin() as conn:
        conn.execute(
            update(bug_reports)
            .where(bug_reports.c.id == bug_id)
            .values(status="open", resolved_at=None)
        )
    warn = await _push_issue_state(bug_id, "open")
    return _redirect(warn)


@router.post("/bugs/{bug_id}/delete")
async def delete_bug(bug_id: int):
    # Close the linked issue first (we lose the number once the row is gone).
    warn = await _push_issue_state(bug_id, "closed")
    with engine.begin() as conn:
        conn.execute(bug_reports.delete().where(bug_reports.c.id == bug_id))
    return _redirect(warn)


@router.post("/bugs/sync")
async def sync_bugs():
    """Pull each tracked issue's state from GitHub and reconcile local status."""
    if not github_issues.is_configured():
        return _redirect("GitHub integration not configured.")

    with engine.connect() as conn:
        rows = [row_to_dict(r) for r in conn.execute(
            bug_reports.select().where(bug_reports.c.gh_issue_number.isnot(None))
        )]

    updated = 0
    errors = []
    for r in rows:
        try:
            state = await run_in_threadpool(github_issues.get_issue_state, r["gh_issue_number"])
        except Exception as e:
            errors.append(f"#{r['gh_issue_number']}: {e}")
            continue
        new_status = "resolved" if state == "closed" else "open"
        if new_status != r["status"]:
            with engine.begin() as conn:
                conn.execute(
                    update(bug_reports)
                    .where(bug_reports.c.id == r["id"])
                    .values(
                        status=new_status,
                        resolved_at=datetime.utcnow() if new_status == "resolved" else None,
                    )
                )
            updated += 1

    warn = "; ".join(errors) if errors else ""
    return _redirect(warn, extra=f"synced=1&updated={updated}")
