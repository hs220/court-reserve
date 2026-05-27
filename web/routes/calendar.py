from datetime import datetime, timedelta

from fastapi import APIRouter
from fastapi.responses import Response

from web.database import engine, bookings, accounts, row_to_dict

router = APIRouter()


@router.get("/calendar/{account_id}.ics")
async def ical_feed(account_id: int):
    with engine.connect() as conn:
        acc = conn.execute(accounts.select().where(accounts.c.id == account_id)).fetchone()
        if not acc:
            return Response("Account not found", status_code=404)
        bk_rows = [row_to_dict(r) for r in conn.execute(
            bookings.select()
            .where(bookings.c.account_id == account_id)
            .order_by(bookings.c.confirmed_at.desc())
        )]

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//CourtReserve Manager//EN",
        "X-WR-CALNAME:CourtReserve Bookings",
        "X-WR-TIMEZONE:America/Los_Angeles",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]

    for b in bk_rows:
        date_str = b["date"]        # YYYY-MM-DD
        start_time = b["start_time"]  # HH:MM
        duration_min = int(b.get("duration_min") or 120)
        court_type = b.get("court_type", "Court")

        # parse local start (naive) — store as floating/local in ICS
        start_dt = datetime.strptime(f"{date_str} {start_time}", "%Y-%m-%d %H:%M")
        end_dt = start_dt + timedelta(minutes=duration_min)
        uid = f"booking-{b['id']}@courtreserve"

        def fmt(dt: datetime) -> str:
            return dt.strftime("%Y%m%dT%H%M%S")

        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTART;TZID=America/Los_Angeles:{fmt(start_dt)}",
            f"DTEND;TZID=America/Los_Angeles:{fmt(end_dt)}",
            f"SUMMARY:Tennis Court - {court_type}",
            f"DESCRIPTION:Booked via CourtReserve Manager",
            f"LOCATION:Tennis Court",
            f"DTSTAMP:{fmt(datetime.utcnow())}Z",
            "END:VEVENT",
        ]

    lines.append("END:VCALENDAR")
    ics_content = "\r\n".join(lines) + "\r\n"
    return Response(
        content=ics_content,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="courts_{account_id}.ics"'},
    )
