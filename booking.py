import json
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional
from urllib.parse import quote
import pytz

from playwright.sync_api import Page


class BookingError(Exception):
    """Booking was rejected by CourtReserve."""

class BookingWindowError(BookingError):
    """Date is beyond the organization's advance booking window."""

class NoAvailableCourtsError(BookingError):
    """No courts available for the requested time slot."""

class AlreadyBookedError(BookingError):
    """Account already has a reservation and is restricted from booking more."""

CANCEL_REASONS = [
    "Schedule conflict came up",
    "Plans changed unexpectedly",
    "Not feeling well",
    "Work obligation",
    "Personal emergency",
]


@dataclass
class OrgConfig:
    org_id: str
    scheduler_id: str
    cost_type_id: str
    timezone: str = "America/Los_Angeles"

    @property
    def read_url(self) -> str:
        return f"https://app.courtreserve.com/Online/Reservations/ReadConsolidated/{self.org_id}"

    @property
    def booking_url(self) -> str:
        return f"https://app.courtreserve.com/Online/Reservations/Bookings/{self.org_id}?sId={self.scheduler_id}"


# Backward-compat defaults (used by CLI when config.yaml doesn't specify overrides)
_DEFAULT_ORG_ID = "13233"
_DEFAULT_SCHEDULER_ID = "16983"
_DEFAULT_COST_TYPE_ID = "141205"
_DEFAULT_TZ = "America/Los_Angeles"

# Keep these as module-level names so any existing import of BOOKING_URL still works
ORG_ID = _DEFAULT_ORG_ID
SCHEDULER_ID = _DEFAULT_SCHEDULER_ID
COST_TYPE_ID = _DEFAULT_COST_TYPE_ID
TZ = _DEFAULT_TZ
READ_URL = f"https://app.courtreserve.com/Online/Reservations/ReadConsolidated/{ORG_ID}"
BOOKING_URL = f"https://app.courtreserve.com/Online/Reservations/Bookings/{ORG_ID}?sId={SCHEDULER_ID}"

DEFAULT_ORG_CONFIG = OrgConfig(
    org_id=_DEFAULT_ORG_ID,
    scheduler_id=_DEFAULT_SCHEDULER_ID,
    cost_type_id=_DEFAULT_COST_TYPE_ID,
    timezone=_DEFAULT_TZ,
)


@dataclass
class Slot:
    court_type: str
    start_ms: int       # Unix ms (UTC)
    end_ms: int
    available_courts: int
    available_court_ids: list[int]
    is_wait_list: bool
    timezone: str = field(default=_DEFAULT_TZ)

    @property
    def start_dt(self) -> datetime:
        return datetime.fromtimestamp(self.start_ms / 1000, tz=pytz.timezone(self.timezone))

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


def _build_json_data(target_date: date, org: OrgConfig) -> str:
    tz = pytz.timezone(org.timezone)
    dt = tz.localize(datetime(target_date.year, target_date.month, target_date.day, 12, 0, 0))
    utc_dt = dt.astimezone(timezone.utc)
    payload = {
        "startDate": utc_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "orgId": org.org_id,
        "TimeZone": org.timezone,
        "Date": utc_dt.strftime("%a, %d %b %Y %H:%M:%S GMT"),
        "KendoDate": {"Year": target_date.year, "Month": target_date.month, "Day": target_date.day},
        "UiCulture": "en-US",
        "CostTypeId": org.cost_type_id,
        "CustomSchedulerId": org.scheduler_id,
        "ReservationMinInterval": "60",
    }
    return json.dumps(payload)


def get_available_slots(page: Page, target_date: date, org: OrgConfig = DEFAULT_ORG_CONFIG) -> list[Slot]:
    json_data = _build_json_data(target_date, org)
    post_body = f"sort=&group=&filter=&jsonData={quote(json_data)}"

    response = page.request.post(
        org.read_url,
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": org.booking_url,
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
            timezone=org.timezone,
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


def _classify_booking_error(msg: str) -> BookingError:
    lower = msg.lower()
    if "only allowed to reserve up to" in lower:
        return BookingWindowError(msg)
    if "no available courts" in lower:
        return NoAvailableCourtsError(msg)
    if "restricted to" in lower and ("per day" in lower or "court" in lower):
        return AlreadyBookedError(msg)
    return BookingError(msg)


def _navigate_to_date(page: Page, target_date: date) -> None:
    js_date = f"new Date({target_date.year}, {target_date.month - 1}, {target_date.day})"
    page.evaluate(f"""
        var scheduler = $("#ConsolidatedScheduler").data("kendoScheduler");
        if (scheduler) {{ scheduler.date({js_date}); }}
    """)
    page.wait_for_timeout(3000)


def book_slot(page: Page, org_url: str, slot: Slot, duration_minutes: int = 60, org: OrgConfig = DEFAULT_ORG_CONFIG, dry_run: bool = False) -> bool:
    print(f"Booking {slot.court_type} court at {slot.start_time} on {slot.start_date} for {duration_minutes} min...")

    # Ensure we're on the booking page
    if org_url not in page.url:
        page.goto(org_url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(2000)

    # Navigate the Kendo Scheduler to the target date
    _navigate_to_date(page, slot.start_dt.date())

    slot_label = slot.start_dt.strftime("%-I:%M %p").upper()  # e.g. "9:30 AM"

    # Find the time cell, scroll it into view, then return its viewport coordinates.
    # Without scrollIntoView, cells below the fold have y > viewport height and the
    # mouse click lands off-screen, producing no modal.
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
            cell.scrollIntoView({{behavior: "instant", block: "center"}});
            var r = cell.getBoundingClientRect();
            return {{x: r.x + r.width / 2, y: r.y + r.height / 2}};
        }})()
    """)

    if not bbox:
        print(f"Could not find time slot for {slot_label} in the scheduler grid.")
        return False

    page.wait_for_timeout(300)  # let scroll settle
    page.mouse.click(bbox['x'], bbox['y'])
    page.wait_for_timeout(2000)
    return _handle_booking_modal(page, slot, duration_minutes, dry_run=dry_run)


def _handle_booking_modal(page: Page, slot: Slot, duration_minutes: int, dry_run: bool = False) -> bool:
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

    # Set duration via Kendo DropDownList.
    # duration_minutes=0 means "any" — leave the widget at its default.
    if duration_minutes > 0:
        duration_options = page.evaluate("""(function() {
            var w = $("#Duration").data("kendoDropDownList");
            if (!w) return [];
            var vf = w.options.dataValueField;
            var opts = [];
            var ds = w.dataSource.data();
            for (var i = 0; i < ds.length; i++) {
                opts.push(String(ds[i][vf] !== undefined ? ds[i][vf] : ds[i][w.options.dataTextField]));
            }
            return opts;
        })()""")

        target_str = str(duration_minutes)
        if target_str in duration_options:
            kendo_value = target_str
        elif duration_options:
            kendo_value = min(duration_options, key=lambda v: abs(int(v) - duration_minutes) if v.isdigit() else 9999)
            print(f"Duration {duration_minutes} not in options {duration_options} — using nearest: {kendo_value}")
        else:
            kendo_value = target_str

        page.evaluate(f"""(function() {{
            var w = $("#Duration").data("kendoDropDownList");
            if (w) {{ w.value("{kendo_value}"); w.trigger("change"); }}
        }})()""")
        page.wait_for_timeout(500)

        end_time = page.evaluate("(function(){ return document.getElementById('EndTime')?.value; })()")
        print(f"Duration={duration_minutes} min (kendo={kendo_value})  EndTime={end_time}  AvailableOptions={duration_options}")
    else:
        print("Duration=any — using CourtReserve default")

    if dry_run:
        print("DRY RUN — stopping before Save. Modal is open; close the browser to exit.")
        page.wait_for_timeout(8000)
        return False

    # Accept the disclosure checkbox
    page.evaluate("""(function() {
        var cb = document.getElementById("DisclosureAgree");
        if (cb && !cb.checked) { cb.checked = true; cb.dispatchEvent(new Event("change", {bubbles: true})); }
    })()""")
    page.wait_for_timeout(300)

    save_btn.click()
    page.wait_for_timeout(4000)

    # Check for error notice/popup (CourtReserve shows a pnotify/swal dialog on failure).
    # Do NOT include generic modal selectors here — they match the booking form itself.
    notice = page.query_selector(
        '.pnotify, .ui-pnotify, [class*="pnotify"], '
        '.sweetalert, .swal2-container'
    )
    if notice:
        msg = notice.inner_text().strip()
        if msg:
            if "reservation confirmed" in msg.lower():
                print("Booking confirmed! (popup)")
                return True
            err = _classify_booking_error(msg)
            print(f"Booking blocked [{type(err).__name__}]: {msg}")
            raise err

    # Broader page text check for inline error messages
    page_text = page.evaluate("(function(){ return document.body.innerText; })()")
    if "Reservation Confirmed" in page_text:
        print("Booking confirmed!")
        return True
    for marker in ["is only allowed", "not allowed", "cannot reserve", "restricted to", "no available courts"]:
        if marker in page_text:
            idx = page_text.find(marker)
            snippet = page_text[max(0, idx-30):idx+120].strip()
            err = _classify_booking_error(snippet)
            print(f"Booking blocked [{type(err).__name__}]: {snippet}")
            raise err

    # Check if booking page still shows an open modal (failure) vs. closed (success)
    open_modal = page.query_selector("#create-res-modal:visible, .modal.show #create-res-modal")
    if open_modal:
        visible_text = open_modal.inner_text().strip()
        if visible_text:
            print(f"Modal still open after Save — possible error: {visible_text[:200]}")
            return False

    print("Booking confirmed!")
    return True


def get_my_reservations(page: Page, org: OrgConfig) -> list[dict]:
    """Fetch upcoming reservations for the logged-in account from CourtReserve."""
    captured = {}

    def _on_response(resp):
        if "my-bookings-portal/get-list" in resp.url:
            try:
                captured["body"] = resp.text()
            except Exception:
                pass

    page.on("response", _on_response)
    page.goto(
        f"https://app.courtreserve.com/Online/Bookings/List/{org.org_id}?type=1",
        wait_until="networkidle",
        timeout=30000,
    )
    page.wait_for_timeout(2000)

    raw_body = captured.get("body")
    if not raw_body:
        return []

    try:
        payload = json.loads(raw_body)
        items = payload.get("Data") or []
    except Exception:
        return []

    results = []
    for item in items:
        if item.get("IsCanceled"):
            continue
        start_str = item.get("ReservationStartDateTime", "")
        end_str   = item.get("ReservationEndDateTime", "")
        if not start_str:
            continue
        try:
            start_dt = datetime.fromisoformat(start_str)
            end_dt   = datetime.fromisoformat(end_str) if end_str else None
        except ValueError:
            continue
        duration_min = int((end_dt - start_dt).total_seconds() / 60) if end_dt else 0
        results.append({
            "reservation_id": str(item.get("ReservationId") or ""),
            "date": start_dt.strftime("%Y-%m-%d"),
            "start_time": start_dt.strftime("%H:%M"),
            "duration_min": duration_min,
            "court_type": item.get("CourtsDisplay") or "",
        })

    return results


def cancel_reservation(page: Page, reservation_id: str, org: OrgConfig = DEFAULT_ORG_CONFIG) -> bool:
    reason = random.choice(CANCEL_REASONS)
    detail_url = f"https://app.courtreserve.com/Online/MyProfile/Reservation/{org.org_id}/{reservation_id}"
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
