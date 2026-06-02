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


@router.post("/bugs/{bug_id}/resolve")
async def resolve_bug(bug_id: int):
    with engine.begin() as conn:
        conn.execute(
            update(bug_reports)
            .where(bug_reports.c.id == bug_id)
            .values(status="resolved", resolved_at=datetime.utcnow())
        )
    return RedirectResponse("/bugs", status_code=303)


@router.post("/bugs/{bug_id}/reopen")
async def reopen_bug(bug_id: int):
    with engine.begin() as conn:
        conn.execute(
            update(bug_reports)
            .where(bug_reports.c.id == bug_id)
            .values(status="open", resolved_at=None)
        )
    return RedirectResponse("/bugs", status_code=303)


@router.post("/bugs/{bug_id}/delete")
async def delete_bug(bug_id: int):
    with engine.begin() as conn:
        conn.execute(bug_reports.delete().where(bug_reports.c.id == bug_id))
    return RedirectResponse("/bugs", status_code=303)
