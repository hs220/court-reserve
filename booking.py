import json
import random
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional
from urllib.parse import quote
import pytz

from playwright.sync_api import Page

CANCEL_REASONS = [
    "Schedule conflict came up",
    "Plans changed unexpectedly",
    "Not feeling well",
    "Work obligation",
    "Personal emergency",
]

ORG_ID = "13233"
SCHEDULER_ID = "16983"
COST_TYPE_ID = "141205"
TZ = "America/Los_Angeles"
READ_URL = f"https://app.courtreserve.com/Online/Reservations/ReadConsolidated/{ORG_ID}"
BOOKING_URL = f"https://app.courtreserve.com/Online/Reservations/Bookings/{ORG_ID}?sId={SCHEDULER_ID}"


@dataclass
class Slot:
    court_type: str
    start_ms: int       # Unix ms (UTC)
    end_ms: int
    available_courts: int
    available_court_ids: list[int]
    is_wait_list: bool

    @property
    def start_dt(self) -> datetime:
        return datetime.fromtimestamp(self.start_ms / 1000, tz=pytz.timezone(TZ))

    @property
    def start_time(self) -> str:
        return self.start_dt.strftime("%H:%M")

    @property
    def start_date(self) -> str:
        return self.start_dt.strftime("%Y-%m-%d")

    def __str__(self):
        return f"{self.start_time}  {self.court_type}  ({self.available_courts} courts available)"


def _parse_ms(val: str) -> int:
    # CourtReserve uses /Date(1234567890000)/ format
    return int(val.replace("/Date(", "").replace(")/", ""))


def _build_json_data(target_date: date) -> str:
    la = pytz.timezone(TZ)
    dt = la.localize(datetime(target_date.year, target_date.month, target_date.day, 12, 0, 0))
    utc_dt = dt.astimezone(timezone.utc)
    payload = {
        "startDate": utc_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "orgId": ORG_ID,
        "TimeZone": TZ,
        "Date": utc_dt.strftime("%a, %d %b %Y %H:%M:%S GMT"),
        "KendoDate": {"Year": target_date.year, "Month": target_date.month, "Day": target_date.day},
        "UiCulture": "en-US",
        "CostTypeId": COST_TYPE_ID,
        "CustomSchedulerId": SCHEDULER_ID,
        "ReservationMinInterval": "60",
    }
    return json.dumps(payload)


def get_available_slots(page: Page, target_date: date) -> list[Slot]:
    json_data = _build_json_data(target_date)
    post_body = f"sort=&group=&filter=&jsonData={quote(json_data)}"

    response = page.request.post(
        READ_URL,
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": BOOKING_URL,
        },
        data=post_body,
    )
    data = response.json()

    slots = []
    for item in data.get("Data", []):
        if item.get("IsClosed") or item.get("IsInPast"):
            continue
        available = item.get("AvailableCourts", 0)
        if available <= 0 and not item.get("IsWaitListSlot"):
            continue
        slots.append(Slot(
            court_type=item["CourtType"],
            start_ms=_parse_ms(item["Start"]),
            end_ms=_parse_ms(item["End"]),
            available_courts=available,
            available_court_ids=item.get("AvailableCourtIds", []),
            is_wait_list=item.get("IsWaitListSlot", False),
        ))
    return slots


def find_best_slot(slots: list[Slot], preferred_times: list[str]) -> Optional[Slot]:
    if not slots:
        return None
    for ptime in (preferred_times or []):
        for s in slots:
            if s.start_time == ptime and not s.is_wait_list:
                return s
    # return first non-waitlist slot
    for s in slots:
        if not s.is_wait_list:
            return s
    return slots[0]


def _navigate_to_date(page: Page, target_date: date) -> None:
    js_date = f"new Date({target_date.year}, {target_date.month - 1}, {target_date.day})"
    page.evaluate(f"""
        var scheduler = $("#ConsolidatedScheduler").data("kendoScheduler");
        if (scheduler) {{ scheduler.date({js_date}); }}
    """)
    page.wait_for_timeout(3000)


def book_slot(page: Page, org_url: str, slot: Slot, duration_minutes: int = 60) -> bool:
    print(f"Booking {slot.court_type} court at {slot.start_time} on {slot.start_date} for {duration_minutes} min...")

    # Ensure we're on the booking page
    if org_url not in page.url:
        page.goto(org_url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(2000)

    # Navigate the Kendo Scheduler to the target date
    _navigate_to_date(page, slot.start_dt.date())

    slot_label = slot.start_dt.strftime("%-I:%M %p").upper()  # e.g. "9:30 AM"

    # Find the time label row index and compute the content row offset.
    # Time label rows start with 2 empty rows before the first actual label.
    bbox = page.evaluate(f"""
        (function() {{
            var timeRows = document.querySelectorAll(".k-scheduler-times tr");
            var firstLabelIdx = -1, targetLabelIdx = -1;
            for (var i = 0; i < timeRows.length; i++) {{
                var txt = timeRows[i].innerText.trim();
                if (firstLabelIdx < 0 && txt && txt !== '\\u200b') firstLabelIdx = i;
                if (txt.toUpperCase() === "{slot_label}") {{ targetLabelIdx = i; break; }}
            }}
            if (targetLabelIdx < 0 || firstLabelIdx < 0) return null;
            var contentRowIdx = targetLabelIdx - firstLabelIdx;
            var contentRows = document.querySelectorAll(".k-scheduler-content table tbody tr");
            if (contentRowIdx >= contentRows.length) return null;
            var cell = contentRows[contentRowIdx].querySelector("td");
            if (!cell) return null;
            var r = cell.getBoundingClientRect();
            return {{x: r.x + r.width / 2, y: r.y + r.height / 2}};
        }})()
    """)

    if not bbox:
        print(f"Could not find time slot for {slot_label} in the scheduler grid.")
        return False

    page.mouse.click(bbox['x'], bbox['y'])
    page.wait_for_timeout(2000)
    return _handle_booking_modal(page, slot, duration_minutes)


def _handle_booking_modal(page: Page, slot: Slot, duration_minutes: int) -> bool:
    modal = page.query_selector("#create-res-modal, .modal-content")
    if not modal:
        print("No booking modal appeared after clicking slot.")
        return False
    print("Booking modal opened — waiting for full AJAX load...")

    # The modal loads in two hops:
    #   1. app.courtreserve.com returns an HTML skeleton + JS that fires a second AJAX call
    #   2. reservations.courtreserve.com returns the actual form HTML into #main-reservation-container
    # Wait for #main-reservation-container to appear — it only exists after hop #2 completes.
    page.wait_for_selector("#main-reservation-container", state="attached", timeout=15000)
    # Give Kendo widgets time to initialize after the form HTML is injected
    page.wait_for_timeout(1500)

    # Confirm Save button is present (modal fully rendered)
    save_btn = page.wait_for_selector('button[type="button"]:has-text("Save")', state="visible", timeout=5000)

    # Set duration via Kendo DropDownList API
    page.evaluate(f"""(function() {{
        var w = $("#Duration").data("kendoDropDownList");
        if (w) {{ w.value("{duration_minutes}"); w.trigger("change"); }}
    }})()""")
    page.wait_for_timeout(500)

    end_time = page.evaluate("(function(){ return document.getElementById('EndTime')?.value; })()")
    print(f"Duration={duration_minutes} min  EndTime={end_time}")

    # Accept the disclosure checkbox
    page.evaluate("""(function() {
        var cb = document.getElementById("DisclosureAgree");
        if (cb && !cb.checked) { cb.checked = true; cb.dispatchEvent(new Event("change", {bubbles: true})); }
    })()""")
    page.wait_for_timeout(300)

    save_btn.click()
    page.wait_for_timeout(4000)

    # Check for error notice/popup (CourtReserve shows a "Reservation Notice" dialog on failure)
    notice = page.query_selector(
        '.pnotify, .ui-pnotify, [class*="pnotify"], '
        '.sweetalert, .swal2-container, '
        '.modal.in .modal-body, .modal:visible .modal-body'
    )
    if notice:
        msg = notice.inner_text().strip()
        if msg and "reservation" in msg.lower():
            print(f"Booking blocked: {msg}")
            return False

    # Broader page text check for inline error messages
    page_text = page.evaluate("(function(){ return document.body.innerText; })()")
    if "Reservation Confirmed" in page_text:
        print("Booking confirmed!")
        return True
    for marker in ["is only allowed", "not allowed", "cannot reserve"]:
        if marker in page_text:
            idx = page_text.find(marker)
            snippet = page_text[max(0, idx-30):idx+120].strip()
            print(f"Booking blocked: {snippet}")
            return False

    # Check if booking page still shows an open modal (failure) vs. closed (success)
    open_modal = page.query_selector("#create-res-modal:visible, .modal.show #create-res-modal")
    if open_modal:
        visible_text = open_modal.inner_text().strip()
        if visible_text:
            print(f"Modal still open after Save — possible error: {visible_text[:200]}")
            return False

    print("Booking confirmed!")
    return True


def cancel_reservation(page: Page, reservation_id: str) -> bool:
    reason = random.choice(CANCEL_REASONS)
    detail_url = f"https://app.courtreserve.com/Online/MyProfile/Reservation/{ORG_ID}/{reservation_id}"
    print(f"Cancelling reservation {reservation_id} (reason: '{reason}')...")

    page.goto(detail_url, wait_until="domcontentloaded", timeout=20000)
    page.wait_for_timeout(2000)

    page.get_by_text("Cancel Reservation", exact=True).first.click()
    page.wait_for_timeout(2000)

    # Fill cancellation reason (required)
    reason_field = page.wait_for_selector("#SelectedReservation_CancellationReason", timeout=5000)
    reason_field.fill(reason)
    page.wait_for_timeout(300)

    page.query_selector('button[type="submit"]:has-text("Cancel Reservation")').click()
    page.wait_for_timeout(4000)

    page_text = page.evaluate("document.body.innerText")
    if "cancelled" in page_text.lower() or "canceled" in page_text.lower() or reservation_id not in page.url:
        print("Cancellation confirmed.")
        return True

    # If still on detail page check for error
    if reservation_id in page.url:
        print(f"Cancellation may have failed. Page text: {page_text[:200]}")
        return False

    print("Cancellation submitted.")
    return True
