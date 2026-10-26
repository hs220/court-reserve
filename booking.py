import json
import random
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
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

class CourtSelectionRequiredError(BookingError):
    """No single court is free for the entire requested window, so CourtReserve leaves
    the court picker empty and rejects Save with "Please select a court". This is NOT a
    terminal failure — it just means a contiguous court isn't available *right now*, so
    the caller should keep watching until one frees up for the whole window."""

class FeedNotJsonError(json.JSONDecodeError):
    """ReadConsolidated answered with something that isn't the slot feed: an HTML error
    page, a Cloudflare challenge, an expired-session redirect, or an app-level gate such
    as an unsigned waiver.

    It subclasses json.JSONDecodeError on purpose so the existing retry classification in
    net_errors.py keeps treating it as transient. What it adds is a message you can act
    on: run #331 logged "Expecting value: line 3 column 1 (char 4)" 1,151 times over 23
    hours, which says nothing about *why* — the answer (a waiver waiting to be signed)
    was sitting in the response body we threw away."""


class ReservationListError(Exception):
    """The account's reservation list couldn't be read: the list response never arrived
    or wasn't the JSON it should be. Distinct from an empty list on purpose -- a sync
    that took this for "no reservations" deleted every upcoming booking it knew of."""


class SlotNotBookableError(BookingError):
    """Clicking the slot cell never opened the create-reservation modal, even after
    several retries. The cell exists in the grid but isn't bookable — typically the
    ReadConsolidated feed advertised availability that's already gone (stale during the
    release rush). The caller should DEMOTE this slot (stop re-picking it) and try
    another candidate rather than re-clicking the same dead cell."""

class PageChangedError(Exception):
    """A CourtReserve page isn't the one this code drives: something it needs to find
    there is gone. Retrying won't help -- the code has to be updated -- so the message
    starts "page changed:" and says what was missing (and must never contain a word that
    net_errors reads as transient, such as "timeout").

    Seen live on 2026-10-24: the booking and reservation pages moved to CourtReserve's
    new React portal (ClientApp/pages/bundle-portal.js). It has no jQuery, no Kendo
    scheduler and no server-rendered cancel form, so run 412 died on "$ is not defined"
    and transfer #39 waited for a cancellation-reason field that no longer exists."""


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


# Org-specific booking-modal behavior (home project — hard-coded per org).
# Some clubs (e.g. Lifetime Santa Clara) expose a court picker (#CourtId) in the
# reservation modal: it auto-fills with a court free for the WHOLE window, or is
# left empty when none spans it — a reliable "not bookable" signal. Others (e.g.
# Lifetime Sunnyvale) auto-assign the court server-side and leave #CourtId blank
# even for bookable slots, so the picker must NOT be used as a signal there; those
# orgs rely on the slot_has_window pre-filter and the post-Save error backstop.
_COURT_PICKER_ORG_IDS = {"13234"}  # Lifetime Santa Clara


def _org_uses_court_picker(org: "OrgConfig") -> bool:
    return org.org_id in _COURT_PICKER_ORG_IDS


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


_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1>", re.I | re.S)


def _body_summary(response, limit: int = 200) -> str:
    """One-line, log-safe description of a response body: its <title> when it's a page,
    plus the start of its visible text. Best-effort — never raises."""
    try:
        body = response.text()
    except Exception as exc:
        return f"(body unavailable: {exc})"
    title = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
    text = " ".join(_TAG_RE.sub(" ", _SCRIPT_RE.sub(" ", body)).split())
    parts = []
    if title:
        parts.append(f"title={' '.join(title.group(1).split())!r}")
    if text:
        parts.append(text[:limit] + ("…" if len(text) > limit else ""))
    return " | ".join(parts) or "(empty body)"


def _feed_json(response):
    """response.json(), but turn a non-JSON body into an error that says what came back."""
    try:
        return response.json()
    except json.JSONDecodeError as exc:
        raise FeedNotJsonError(
            f"HTTP {response.status} from ReadConsolidated was not JSON — {_body_summary(response)}",
            exc.doc, exc.pos,
        ) from exc


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
    data = _feed_json(response)

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


_INTERVAL_MS = 30 * 60 * 1000  # CourtReserve scheduler granularity


def slot_has_window(slots: list[Slot], start: Slot, duration_minutes: int) -> bool:
    """Heuristic pre-filter: True if a *single* court appears free across enough
    consecutive intervals to cover `duration_minutes` starting at `start`.

    CourtReserve's ReadConsolidated feed reports availability per 30-min interval, and
    a reservation must hold the same court for its whole length. This intersects each
    interval's AvailableCourtIds to cheaply reject slots that obviously can't fit (e.g.
    an interval is missing entirely), avoiding a wasted modal open. It is NOT
    authoritative, though — a court can appear in every interval's list yet still not be
    bookable as one contiguous block, so the feed can yield false positives. The real
    test is the modal's #CourtId picker (see _handle_booking_modal), which raises
    CourtSelectionRequiredError when no court spans the window. Returns True when
    duration_minutes <= 0 (no constraint)."""
    if duration_minutes <= 0:
        return True
    by_start = {s.start_ms: s for s in slots}
    interval = (start.end_ms - start.start_ms) or _INTERVAL_MS
    needed = (duration_minutes * 60_000 + interval - 1) // interval  # ceil
    common: Optional[set] = None
    for k in range(needed):
        s = by_start.get(start.start_ms + k * interval)
        if s is None or s.is_wait_list:
            return False
        ids = set(s.available_court_ids)
        common = ids if common is None else (common & ids)
        if not common:
            return False
    return True


def find_best_slot(slots: list[Slot], preferred_times: list[str], allow_fallback: bool = True,
                   duration_minutes: int = 0, exclude_starts: Optional[set] = None) -> Optional[Slot]:
    if not slots:
        return None

    excluded = exclude_starts or set()

    def ok(s: Slot) -> bool:
        return (s.start_ms not in excluded
                and not s.is_wait_list
                and slot_has_window(slots, s, duration_minutes))

    for ptime in (preferred_times or []):
        for s in slots:
            if s.start_time == ptime and ok(s):
                return s
    # No preferred match — only fall back if allowed
    if not allow_fallback:
        return None
    # return first slot that can actually hold the full duration
    for s in slots:
        if ok(s):
            return s
    # Last resort (any non-excluded slot, incl. waitlist) only when not enforcing
    # a duration window
    if duration_minutes <= 0:
        for s in slots:
            if s.start_ms not in excluded:
                return s
    return None


def _classify_booking_error(msg: str) -> BookingError:
    lower = msg.lower()
    if "only allowed to reserve up to" in lower:
        return BookingWindowError(msg)
    if "select a court" in lower:
        return CourtSelectionRequiredError(msg)
    if "no available courts" in lower:
        return NoAvailableCourtsError(msg)
    if "restricted to" in lower and ("per day" in lower or "court" in lower):
        return AlreadyBookedError(msg)
    return BookingError(msg)


# Which UI a CourtReserve page is: "kendo" (the old booking page, with a bound Kendo
# scheduler), "legacy" (an old server-rendered page with jQuery, e.g. the old reservation
# page), "portal" (the new React portal, marked by its shell and data-testids), or
# "unknown" (still loading, a login or challenge page, or something new again).
_PAGE_KIND_JS = """(function() {
    if (window.jQuery && window.jQuery("#ConsolidatedScheduler").data("kendoScheduler")) return "kendo";
    if (document.querySelector("#react-portal-shell, [data-testid=scheduler-page], "
                               + "[data-testid=reservation-details-page]")) return "portal";
    if (window.jQuery) return "legacy";
    return "unknown";
})()"""


def _page_kind(page: Page, settle_ms: int = 10000,
               until=("kendo", "legacy", "portal")) -> str:
    """The page's kind (see _PAGE_KIND_JS), polling up to settle_ms until it is one of
    `until` -- the portal mounts its markup a moment after domcontentloaded, and an old
    booking page has jQuery a moment before its scheduler is bound. Returns the last
    answer when the window runs out."""
    kind = "unknown"
    for _ in range(max(1, settle_ms // 500)):
        kind = page.evaluate(_PAGE_KIND_JS)
        if kind in until:
            return kind
        page.wait_for_timeout(500)
    return kind


def ensure_booking_page(page: Page, org_url: str) -> str:
    """Be on a booking page this code can drive, before anything is clicked. Returns its
    kind: "kendo" (the old Kendo scheduler) or "portal" (CourtReserve's new React
    portal, [data-testid=scheduler-page]).

    Reloads when the page is elsewhere or has lost its scheduler. It doesn't trust the
    URL alone: run 412's page sat on the booking URL for three hours, and when it came
    to book, the page there had no jQuery at all. Raises PageChangedError when the
    reloaded page is neither."""
    if org_url in page.url:
        kind = page.evaluate(_PAGE_KIND_JS)
        if kind == "kendo" or (kind == "portal" and page.query_selector(_PORTAL_SCHEDULER)):
            return kind  # the pre-warmed page is still good; don't spend a reload at release time
    page.goto(org_url, wait_until="domcontentloaded", timeout=20000)
    # "legacy" isn't an answer here: it's what an old booking page says while jQuery is
    # up but the scheduler isn't bound yet, so keep polling through it.
    kind = _page_kind(page, until=("kendo", "portal"))
    if kind == "kendo":
        return kind
    if kind == "portal":
        # The portal's shell is up before React renders the scheduler into it.
        try:
            page.wait_for_selector(_PORTAL_SCHEDULER, timeout=15000)
        except Exception:
            raise PageChangedError(
                "page changed: the portal's booking page never showed its scheduler "
                "([data-testid=scheduler-page]). Nothing was clicked.") from None
        return kind
    # The URL only, not the title: a "504 Gateway Time-out" title would make net_errors
    # read this as transient and retry it.
    raise PageChangedError(
        f"page changed: no scheduler on the booking page, neither Kendo "
        f"(#ConsolidatedScheduler) nor the portal's (url={page.url}). Nothing was clicked.")


# --- CourtReserve's new portal (React + Ant Design), as recorded live on 2026-10-25 ---
# Class names there are build hashes, so everything below keys on data-testid, ARIA
# roles and visible text.
#   grid:  [data-testid=scheduler-page]; a column per court type under
#          [data-testid=scheduler-header-court-type-<CourtType>]; a 46px cell per
#          half hour; a bookable cell is [data-testid=consolidated-reserve-slot]
#          (role=button, "Reserve"), a taken one a plain div reading "None Available".
#          Time labels are leaf elements reading e.g. "2:30 PM". The day is changed
#          with [aria-label="Previous day"/"Next day"]; .ant-picker's input holds
#          MM/DD/YYYY.
#   form:  "Reserve" opens .ant-modal[role=dialog] "Create Reservation" holding
#          [data-testid=reservation-form]; [data-testid=reservation-date-time-display]
#          reads "OCT 30 Friday 2:30 PM – 3:00 PM ...". Duration is a base-ui
#          button[role=combobox] ("30 minutes") whose [role=option]s are "30 minutes",
#          "1 hour", "1 hour & 30 minutes", "2 hours", backed by a hidden
#          input[name=duration] holding the minutes. "I agree to the terms above" is a
#          styled div (no input, no aria-checked). Buttons: View, Close, Confirm.
#          (Sunnyvale; Santa Clara's court picker hasn't been seen on the portal.)
_PORTAL_SCHEDULER = "[data-testid=scheduler-page]"
_PORTAL_SLOT = "[data-testid=consolidated-reserve-slot]"
_PORTAL_FORM = "[data-testid=reservation-form]"
_PORTAL_MODAL = ".ant-modal[role=dialog]"
_PORTAL_TERMS = "I agree to the terms above"

_PORTAL_DATE_JS = """(function() {
    var i = document.querySelector("[data-testid=scheduler-page] .ant-picker input")
            || document.querySelector(".ant-picker input");
    return i ? i.value : null;
})()"""

# Where the slot's cell is, and what it shows: the cell under the court type's column
# at the height of the slot's time label. A bookable cell is marked data-cr-pick=<token>
# in the same pass, so the click lands on that cell even if the grid re-renders and
# shifts the Reserve buttons in between (live: clicking the Nth one opened the next
# half hour's form).
_PORTAL_FIND_SLOT_JS = """([label, courtType, token]) => {
    const leaf = [...document.querySelectorAll("[data-testid=scheduler-page] *")].find(
        el => el.children.length === 0 && (el.textContent || "").trim().toUpperCase() === label);
    if (!leaf) return null;
    leaf.scrollIntoView({block: "center"});
    const lr = leaf.getBoundingClientRect(), y = lr.y + lr.height / 2;
    const prefix = "scheduler-header-court-type-";
    const heads = [...document.querySelectorAll("[data-testid^='" + prefix + "']")]
        .filter(h => !h.getAttribute("data-testid").startsWith(prefix + "name-"));
    const head = heads.find(h => h.getAttribute("data-testid") === prefix + courtType)
                 || (heads.length === 1 ? heads[0] : null);
    if (!head) return {state: "no column", columns: heads.map(h => h.getAttribute("data-testid").slice(prefix.length))};
    const hr = head.getBoundingClientRect(), x = hr.x + hr.width / 2;
    const slots = [...document.querySelectorAll("[data-testid=consolidated-reserve-slot]")];
    const index = slots.findIndex(s => { const r = s.getBoundingClientRect();
        return r.top <= y && y <= r.bottom && r.left <= x && x <= r.right; });
    if (index >= 0) {
        document.querySelectorAll("[data-cr-pick]").forEach(e => e.removeAttribute("data-cr-pick"));
        slots[index].setAttribute("data-cr-pick", token);
        return {state: "reserve", pick: "[data-cr-pick='" + token + "']"};
    }
    const el = document.elementFromPoint(x, y);
    const text = ((el && el.innerText) || "").trim().toUpperCase();
    return {state: text.includes("NONE AVAILABLE") ? "none available"
                   : text.includes("UNAVAILABLE") ? "unavailable" : "unknown", text: text.slice(0, 40)};
}"""

_PORTAL_FORM_JS = """(function() {
    var m = document.querySelector(".ant-modal[role=dialog]");
    if (!m) return null;
    function txt(el) { return el ? (el.innerText || "").replace(/\\s+/g, " ").trim() : null; }
    var combos = m.querySelectorAll("button[role=combobox]");
    var dur = m.querySelector("input[name=duration]");
    return {
        form: !!m.querySelector("[data-testid=reservation-form]"),
        dateTime: txt(m.querySelector("[data-testid=reservation-date-time-display]")),
        combos: combos.length,
        duration: txt(combos[0]),
        durationValue: dur ? dur.value : null,
        terms: Array.prototype.some.call(m.querySelectorAll("span"),
            function(s) { return (s.textContent || "").trim() === "I agree to the terms above"; }),
    };
})()"""

_PORTAL_SLOT_COUNT_JS = """(function() {
    return document.querySelectorAll("[data-testid=consolidated-reserve-slot]").length;
})()"""

_PORTAL_OPTIONS_JS = """(function() {
    return Array.prototype.map.call(document.querySelectorAll("[role=listbox] [role=option]"),
        function(o) { return (o.innerText || "").trim(); });
})()"""

# Messages the portal shows after Confirm: toasts, notifications, alerts, and form
# errors inside the still-open modal. Never the modal's other text (it names the member).
_PORTAL_NOTICES_JS = """(function() {
    var sel = ".ant-message, .ant-notification, [role=alert], .ant-form-item-explain-error, .ant-modal-confirm-content";
    return Array.prototype.map.call(document.querySelectorAll(sel),
        function(e) { return (e.innerText || "").replace(/\\s+/g, " ").trim().slice(0, 300); })
        .filter(function(t) { return t; });
})()"""


def _portal_duration_label(minutes: int) -> str:
    """The portal's wording for a duration: "30 minutes", "1 hour", "1 hour & 30
    minutes", "2 hours"."""
    h, m = divmod(minutes, 60)
    parts = []
    if h:
        parts.append(f"{h} hour" + ("s" if h > 1 else ""))
    if m:
        parts.append(f"{m} minute" + ("s" if m > 1 else ""))
    return " & ".join(parts)


def _portal_duration_minutes(label: str) -> Optional[int]:
    h = re.search(r"(\d+)\s*hour", label or "")
    m = re.search(r"(\d+)\s*minute", label or "")
    if not h and not m:
        return None
    return (int(h.group(1)) * 60 if h else 0) + (int(m.group(1)) if m else 0)


def _portal_go_to_date(page: Page, target: date) -> None:
    """Step the portal's scheduler to `target` with its Previous/Next day buttons, then
    refresh it, so the grid isn't the one loaded at pre-warm."""
    # Live: [data-testid=scheduler-page] renders a moment before its date picker does.
    for _ in range(20):  # up to ~10s
        if page.evaluate(_PORTAL_DATE_JS):
            break
        page.wait_for_timeout(500)
    for _ in range(62):
        shown = page.evaluate(_PORTAL_DATE_JS)
        try:
            current = datetime.strptime(shown or "", "%m/%d/%Y").date()
        except ValueError:
            raise PageChangedError(
                f"page changed: the portal's scheduler date picker doesn't read as "
                f"MM/DD/YYYY ({shown!r}). Nothing was clicked.") from None
        if current == target:
            break
        page.click('[aria-label="Next day"]' if current < target else '[aria-label="Previous day"]')
        for _ in range(20):  # up to ~5s for the day to change
            page.wait_for_timeout(250)
            if page.evaluate(_PORTAL_DATE_JS) != shown:
                break
    else:
        raise PageChangedError(f"page changed: the portal's scheduler never reached {target}.")
    refresh = page.query_selector("[data-testid=scheduler-refresh-btn]")
    if refresh:
        refresh.click()
    page.wait_for_timeout(1000)
    # Let the refreshed grid settle: the same number of Reserve buttons twice running.
    last = None
    for _ in range(10):  # up to ~5s
        count = page.evaluate(_PORTAL_SLOT_COUNT_JS)
        if count == last:
            break
        last = count
        page.wait_for_timeout(500)


def _book_slot_portal(page: Page, slot: Slot, duration_minutes: int, org: OrgConfig,
                      dry_run: bool, result: Optional[dict]) -> bool:
    """book_slot on CourtReserve's new portal. Same contract as the Kendo path: True when
    booked, False when it didn't complete, and the BookingError subclasses for what the
    caller treats specially. A booking counts only when the account's reservation list
    then shows it -- the portal's own confirmation hasn't been seen yet."""
    slot_label = slot.start_dt.strftime("%-I:%M %p").upper()  # e.g. "2:30 PM"
    _portal_go_to_date(page, slot.start_dt.date())

    when = ""
    for opened in range(2):  # a second try if the form that opens is for another slot
        found = None
        for attempt in range(5):  # the grid can lag the feed; re-read it like the Kendo path
            token = f"{slot.start_ms}-{opened}-{attempt}"
            found = page.evaluate(_PORTAL_FIND_SLOT_JS, [slot_label, slot.court_type, token])
            if found and found.get("state") != "unknown":
                break
            page.wait_for_timeout(2000)
        if not found:
            print(f"Could not find {slot_label} in the portal's scheduler grid.")
            return False
        state = found.get("state")
        if state == "no column":
            raise PageChangedError(f"page changed: no '{slot.court_type}' column in the portal's "
                                   f"scheduler (columns: {found.get('columns')}). Nothing was clicked.")
        if state != "reserve":
            marker = {"none available": "NONE AVAILABLE (taken)",
                      "unavailable": "UNAVAILABLE (not open yet / past)"}.get(state, f"no Reserve button ({found.get('text')!r})")
            print(f"Slot {slot_label} shows '{marker}' on the portal grid — not bookable right now.")
            raise SlotNotBookableError(f"Slot at {slot.start_time} on {slot.start_date} not bookable ({marker}).")

        page.locator(found["pick"]).click(timeout=5000)
        try:
            page.wait_for_selector(_PORTAL_FORM, timeout=15000)
        except Exception:
            raise SlotNotBookableError(f"Slot at {slot.start_time} on {slot.start_date}: 'Reserve' "
                                       f"never opened the reservation form.") from None
        page.wait_for_timeout(1500)
        print("Booking form opened (portal).")

        form = page.evaluate(_PORTAL_FORM_JS) or {}
        # The form must be for this slot: its tile reads "OCT 30 Friday 2:30 PM – 3:00 PM ...".
        when = form.get("dateTime") or ""
        day_tag = f"{slot.start_dt.strftime('%b').upper()} {slot.start_dt.day}"
        if day_tag in when.upper() and f"{slot_label} –" in when.upper():
            break
        print(f"The form that opened is for {when[:40]!r}, not {slot.start_date} {slot_label} — closing it.")
        page.locator(_PORTAL_MODAL).locator(".ant-modal-close").first.click()
        page.wait_for_timeout(1000)
    else:
        raise PageChangedError(f"page changed: the portal's form isn't for {slot.start_date} "
                               f"{slot_label} (it reads {when[:60]!r}). Nothing was confirmed.")
    if form.get("combos", 0) != 1:
        # Sunnyvale's form has one picker, Duration. A second one (a court picker, as the
        # old Santa Clara form had) hasn't been seen on the portal: don't guess at it.
        raise PageChangedError(f"page changed: the portal's form has {form.get('combos')} "
                               f"pickers, and only Duration is mapped. Nothing was confirmed.")

    if duration_minutes > 0:
        want = _portal_duration_label(duration_minutes)
        if form.get("duration") != want:
            page.locator(_PORTAL_MODAL).locator("button[role=combobox]").first.click()
            page.wait_for_timeout(500)
            options = page.evaluate(_PORTAL_OPTIONS_JS) or []
            if want not in options:
                usable = [o for o in options if _portal_duration_minutes(o)]
                if not usable:
                    raise PageChangedError(f"page changed: the portal's Duration list has no "
                                           f"readable options ({options}). Nothing was confirmed.")
                want = min(usable, key=lambda o: abs(_portal_duration_minutes(o) - duration_minutes))
                print(f"Duration {duration_minutes} not in options {options} — using nearest: {want}")
            page.get_by_role("option", name=want, exact=True).click()
            page.wait_for_timeout(800)
            form = page.evaluate(_PORTAL_FORM_JS) or {}
        minutes = _portal_duration_minutes(want)
        end = (slot.start_dt + timedelta(minutes=minutes)).strftime("%-I:%M %p").upper()
        ok = (form.get("duration") == want and form.get("durationValue") == str(minutes)
              and end in (form.get("dateTime") or "").upper())
        print(f"Duration={duration_minutes} min (portal: {form.get('duration')!r}, value="
              f"{form.get('durationValue')})  ends {end}: {'ok' if ok else 'NOT applied'}")
        if not ok:
            print(f"Could not confirm duration {want} — aborting before Confirm to avoid booking the wrong length.")
            return False
    else:
        print(f"Duration=any — using the portal's default ({form.get('duration')!r})")

    if dry_run:
        print("DRY RUN — stopping before Confirm.")
        return False

    modal = page.locator(_PORTAL_MODAL)
    if form.get("terms"):
        modal.get_by_text(_PORTAL_TERMS, exact=True).click()
        page.wait_for_timeout(300)

    capture: dict = {}

    def _on_response(resp):
        if capture.get("id") or not _may_carry_new_reservation_id(resp.url):
            return
        try:
            m = re.search(r'"[Rr]eservationId"\s*:\s*"?(\d{3,})', resp.text() or "")
        except Exception:
            return
        if m:
            capture["id"] = m.group(1)

    page.on("response", _on_response)
    try:
        modal.get_by_role("button", name="Confirm", exact=True).click()
        page.wait_for_timeout(4000)
    finally:
        page.remove_listener("response", _on_response)

    notices = page.evaluate(_PORTAL_NOTICES_JS) or []
    for msg in notices:
        lower = msg.lower()
        if any(k in lower for k in ("only allowed", "not allowed", "cannot reserve", "restricted to",
                                    "no available courts", "select a court")):
            err = _classify_booking_error(msg)
            print(f"Booking blocked [{type(err).__name__}]: {msg}")
            raise err

    # The positive answer: the reservation is on the account's list.
    rid = None
    for attempt in range(3):
        try:
            listed = get_my_reservations(page, org)
        except ReservationListError as exc:
            # Confirm was clicked: this may be booked. Fail loudly rather than let a
            # watch take it for "not booked" and try again.
            raise ReservationListError(f"Confirm was clicked for {slot.start_date} {slot.start_time}, "
                                       f"but the reservation list couldn't be read to check it: {exc}") from exc
        rid = match_reservation_id(listed, slot.start_date, slot.start_time)
        if rid:
            break
        page.wait_for_timeout(2000)
    if not rid:
        print(f"Could not confirm booking: {slot.start_date} {slot.start_time} isn't on the "
              f"reservation list. Portal messages: {notices or 'none'}")
        return False
    if capture.get("id") and capture["id"] != rid:
        print(f"(save response named reservation {capture['id']}; the list says {rid} — using the list)")
    if result is not None:
        result["reservation_id"] = rid
    print(f"Booking confirmed! (on the reservation list as {rid})")
    return True


def _navigate_to_date(page: Page, target_date: date) -> None:
    """Point the Kendo scheduler at target_date AND force a fresh server fetch.

    The grid is loaded once during pre-warm (before the noon release) and Kendo
    won't re-read when we navigate to the same date across retries — so without an
    explicit dataSource.read() the grid keeps showing whatever it loaded earlier
    (e.g. the pre-release "NONE AVAILABLE" state), and we end up clicking a stale,
    un-bookable cell even though the live feed shows the slot open. This forces the
    grid to re-fetch each time, mirroring a manual browser refresh."""
    js_date = f"new Date({target_date.year}, {target_date.month - 1}, {target_date.day})"
    page.evaluate(f"""
        (function() {{
            if (!window.jQuery) return;
            var scheduler = window.jQuery("#ConsolidatedScheduler").data("kendoScheduler");
            if (!scheduler) return;
            window.__crDataBound = false;
            scheduler.one("dataBound", function() {{ window.__crDataBound = true; }});
            scheduler.date({js_date});
            scheduler.dataSource.read();   // force fresh fetch even if date unchanged
        }})()
    """)
    # Wait for the refreshed data to bind, then a short settle for re-render.
    for _ in range(24):  # up to ~6s
        if page.evaluate("window.__crDataBound === true"):
            break
        page.wait_for_timeout(250)
    page.wait_for_timeout(600)


def _find_slot_bbox(page: Page, slot_label: str):
    """Return {x, y, marker} for the Kendo scheduler cell matching slot_label, or None
    if the row/cell isn't in the grid yet. `marker` is None when the cell looks bookable,
    or a short reason string when the (fresh) grid shows it's NOT bookable — i.e. its
    slot container is "not-available-courts-container" / renders "NONE AVAILABLE". The
    caller uses that to demote the slot immediately instead of burning ~10s of click
    retries."""
    return page.evaluate(f"""
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
            var cx = r.x + r.width / 2, cy = r.y + r.height / 2;

            // Kendo renders the slot items in an overlay layer, NOT inside this <td>,
            // so inspect whatever is actually rendered at the click point and walk up
            // to its .consolidate-item-container to read the real bookability state.
            // A bookable slot's container carries "available-courts-container" (and a
            // "Reserve" button); a gone one carries "not-available-courts-container" /
            // "NONE AVAILABLE". NOTE: the Reserve <a> has class "fn-disable" in BOTH
            // cases, so fn-disable is NOT a usable signal — only the container class is.
            var marker = null;
            var el = document.elementFromPoint(cx, cy);
            var node = el, container = null;
            for (var d = 0; d < 6 && node; d++) {{
                if ((" " + (node.className || "") + " ").indexOf("consolidate-item-container") >= 0) {{
                    container = node; break;
                }}
                node = node.parentElement;
            }}
            // Three rendered states (read off the container class, which is reliable;
            // text is a fallback):
            //   available-courts-container  -> "Reserve"        -> BOOKABLE (marker null)
            //   not-available-courts-container -> "NONE AVAILABLE" -> taken
            //   inPast-courts-container     -> "UNAVAILABLE"    -> not open yet / past
            // (NOTE: "not-available-courts-container" contains the substring
            // "available-courts-container", so check the negative classes first.)
            var target = container || el;
            if (target) {{
                var tcls = " " + (target.className || "") + " ";
                var ttxt = (target.innerText || "").toUpperCase();
                if (tcls.indexOf("not-available-courts-container") >= 0 ||
                    ttxt.indexOf("NONE AVAILABLE") >= 0) {{
                    marker = "NONE AVAILABLE (taken)";
                }} else if (tcls.indexOf("inPast-courts-container") >= 0 ||
                           ttxt.indexOf("UNAVAILABLE") >= 0) {{
                    marker = "UNAVAILABLE (not open yet / past)";
                }}
            }}
            return {{x: cx, y: cy, marker: marker}};
        }})()
    """)


def book_slot(page: Page, org_url: str, slot: Slot, duration_minutes: int = 60, org: OrgConfig = DEFAULT_ORG_CONFIG, dry_run: bool = False, result: Optional[dict] = None) -> bool:
    ts = datetime.now(pytz.timezone("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M:%S %Z")
    print(f"[{ts}] Booking {slot.court_type} court at {slot.start_time} on {slot.start_date} for {duration_minutes} min...")

    # Be on a booking page with a live scheduler (raises PageChangedError if it's gone)
    if ensure_booking_page(page, org_url) == "portal":
        return _book_slot_portal(page, slot, duration_minutes, org, dry_run, result)

    # Navigate the Kendo Scheduler to the target date
    _navigate_to_date(page, slot.start_dt.date())

    slot_label = slot.start_dt.strftime("%-I:%M %p").upper()  # e.g. "9:30 AM"

    # Kendo grid may lag behind the API — retry navigation until the cell appears.
    # Without scrollIntoView, cells below the fold have y > viewport height and the
    # mouse click lands off-screen, producing no modal.
    GRID_RETRIES = 5
    bbox = None
    for grid_attempt in range(GRID_RETRIES):
        bbox = _find_slot_bbox(page, slot_label)
        if bbox:
            break
        ts = datetime.now(pytz.timezone("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M:%S %Z")
        if grid_attempt < GRID_RETRIES - 1:
            print(f"[{ts}] {slot_label} not in grid yet (attempt {grid_attempt + 1}/{GRID_RETRIES}) — re-navigating in 5s...")
            page.wait_for_timeout(5000)
            _navigate_to_date(page, slot.start_dt.date())
        else:
            print(f"[{ts}] Could not find time slot for {slot_label} in the scheduler grid after {GRID_RETRIES} attempts.")
            return False

    # The freshly-read grid already tells us if the cell isn't bookable (renders
    # "NONE AVAILABLE" or a disabled Reserve button). Demote immediately rather than
    # wasting ~10s clicking a cell that will never open a modal — so the caller can
    # cycle through the other preferred times (or re-try this one on fresh data) fast.
    if bbox.get("marker"):
        ts = datetime.now(pytz.timezone("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M:%S %Z")
        print(f"[{ts}] Slot {slot_label} shows '{bbox['marker']}' on fresh grid — not bookable right now.")
        raise SlotNotBookableError(
            f"Slot at {slot.start_time} on {slot.start_date} not bookable "
            f"({bbox['marker']}).")

    # Clicking the cell occasionally fails to open the modal (the AJAX call
    # doesn't fire, or the click lands a hair off the cell). Retry the click a
    # few times before giving up — re-finding the cell each time in case the
    # grid re-rendered underneath us.
    CLICK_RETRIES = 5
    for click_attempt in range(CLICK_RETRIES):
        page.wait_for_timeout(300)  # let scroll settle
        page.mouse.click(bbox['x'], bbox['y'])
        page.wait_for_timeout(2000)

        if page.query_selector("#create-res-modal, .modal-content"):
            return _handle_booking_modal(page, slot, duration_minutes, org=org, dry_run=dry_run, result=result)

        ts = datetime.now(pytz.timezone("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M:%S %Z")
        if click_attempt < CLICK_RETRIES - 1:
            print(f"[{ts}] No booking modal appeared after clicking slot "
                  f"(attempt {click_attempt + 1}/{CLICK_RETRIES}) — retrying...")
            # Re-locate the cell; the grid may have shifted or re-rendered. If it now
            # shows unbookable, demote fast instead of finishing the retries.
            new_bbox = _find_slot_bbox(page, slot_label)
            if new_bbox and new_bbox.get("marker"):
                raise SlotNotBookableError(
                    f"Slot at {slot.start_time} on {slot.start_date} became not "
                    f"bookable ({new_bbox['marker']}).")
            if new_bbox:
                bbox = new_bbox
        else:
            print(f"[{ts}] No booking modal appeared after clicking slot "
                  f"after {CLICK_RETRIES} attempts.")
            _dump_no_modal_diagnostics(page, slot_label, bbox)
            raise SlotNotBookableError(
                f"Slot at {slot.start_time} on {slot.start_date} clicked but never "
                f"opened a booking modal — cell not bookable (likely stale feed).")

    raise SlotNotBookableError(
        f"Slot at {slot.start_time} on {slot.start_date} not bookable.")


def _dump_no_modal_diagnostics(page: Page, slot_label: str, bbox) -> None:
    """On a persistent no-modal failure, capture page state so we can tell whether
    the click missed the cell (cause: off-screen/wrong element) or a modal opened
    that our selector didn't match (cause: CourtReserve markup changed)."""
    import os, time as _time
    stamp = _time.strftime("%Y%m%d-%H%M%S")
    # Write to DATA_DIR (the mounted volume on NAS) so the screenshot survives the
    # container and is retrievable; fall back to /tmp for local/CLI runs.
    out_dir = os.environ.get("DATA_DIR", "/tmp")
    base = os.path.join(out_dir, f"court-debug-{stamp}")
    try:
        page.screenshot(path=f"{base}.png", full_page=True)
        print(f"  [debug] screenshot -> {base}.png")
    except Exception as e:
        print(f"  [debug] screenshot failed: {e}")
    try:
        bx = (bbox or {}).get("x", -1)
        by = (bbox or {}).get("y", -1)
        info = page.evaluate("""(function(pt) {
            var bx = pt[0], by = pt[1];
            var vp = {w: window.innerWidth, h: window.innerHeight,
                      scrollY: window.scrollY};
            // Any dialog-like containers currently in the DOM
            var sels = ["#create-res-modal", ".modal-content", ".modal", ".k-window",
                        ".k-dialog", "[role=dialog]", "#main-reservation-container",
                        ".pnotify", ".swal2-container"];
            var found = {};
            sels.forEach(function(s) {
                var els = document.querySelectorAll(s);
                if (els.length) {
                    found[s] = [];
                    els.forEach(function(el) {
                        var r = el.getBoundingClientRect();
                        found[s].push({visible: !!(el.offsetParent) ,
                                       w: Math.round(r.width), h: Math.round(r.height),
                                       cls: el.className});
                    });
                }
            });
            // What element is actually at the click point?
            var atPoint = null;
            if (bx >= 0) {
                var e = document.elementFromPoint(bx, by);
                if (e) atPoint = {tag: e.tagName, cls: e.className,
                                  txt: (e.innerText||"").slice(0,60)};
            }
            return {viewport: vp, dialogs: found, atClickPoint: atPoint, url: location.href};
        })""", [bx, by])
        print(f"  [debug] url={info.get('url')}")
        print(f"  [debug] viewport={info.get('viewport')}  clickPoint={bbox}")
        print(f"  [debug] elementAtClickPoint={info.get('atClickPoint')}")
        print(f"  [debug] dialog-like containers found: {json.dumps(info.get('dialogs'), indent=2)}")
    except Exception as e:
        print(f"  [debug] dom probe failed: {e}")


def _may_carry_new_reservation_id(url: str) -> bool:
    """Whether a response seen after Save may name the reservation just made. Anything
    reservation- or booking-ish, except the grid's slot feed (Reservations/
    ReadConsolidated): it matches "reservation" too, and a grid refresh after Save would
    carry other reservations' ids. A transfer cancels whatever id is captured, so the
    feed is never a source."""
    u = url.lower()
    return ("reservation" in u or "booking" in u) and "readconsolidated" not in u


def _handle_booking_modal(page: Page, slot: Slot, duration_minutes: int, org: OrgConfig = DEFAULT_ORG_CONFIG, dry_run: bool = False, result: Optional[dict] = None) -> bool:
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
        _GET_DURATION_OPTIONS = """(function() {
            var w = $("#Duration").data("kendoDropDownList");
            if (!w) return [];
            var vf = w.options.dataValueField;
            var ds = w.dataSource.data();
            var opts = [];
            for (var i = 0; i < ds.length; i++) {
                opts.push(String(ds[i][vf] !== undefined ? ds[i][vf] : ds[i][w.options.dataTextField]));
            }
            return opts;
        })()"""

        # The Kendo widget may still be initializing — wait up to 3s for it to populate.
        duration_options = []
        for _wait_attempt in range(6):
            duration_options = page.evaluate(_GET_DURATION_OPTIONS)
            if duration_options:
                break
            page.wait_for_timeout(500)

        target_str = str(duration_minutes)
        if target_str in duration_options:
            kendo_value = target_str
        elif duration_options:
            kendo_value = min(duration_options, key=lambda v: abs(int(v) - duration_minutes) if v.isdigit() else 9999)
            print(f"Duration {duration_minutes} not in options {duration_options} — using nearest: {kendo_value}")
        else:
            kendo_value = target_str

        # CourtReserve's Duration widget re-binds its dataSource asynchronously
        # after the modal loads. A value set too early can silently revert to the
        # default (60 min) even while every option still appears in the list — so
        # the form ends up submitting the wrong length (e.g. a 2-hour request
        # books only 1 hour). Set the value, then verify the form's actual EndTime
        # matches the requested duration, re-applying if the widget reverted.
        expected_end = (slot.start_dt + timedelta(minutes=int(kendo_value))).strftime("%-I:%M %p")

        def _norm_time(s: Optional[str]) -> str:
            return (s or "").strip().upper().lstrip("0")

        end_time = None
        actual_value = None
        duration_ok = False
        for set_attempt in range(5):
            page.evaluate(f"""(function() {{
                var w = $("#Duration").data("kendoDropDownList");
                if (w) {{ w.value("{kendo_value}"); w.trigger("change"); }}
            }})()""")
            page.wait_for_timeout(500)

            end_time = page.evaluate("(function(){ return document.getElementById('EndTime')?.value; })()")
            actual_value = page.evaluate("""(function() {
                var w = $("#Duration").data("kendoDropDownList");
                return w ? String(w.value()) : null;
            })()""")
            if actual_value == kendo_value and _norm_time(end_time) == _norm_time(expected_end):
                duration_ok = True
                break
            ts = datetime.now(pytz.timezone("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M:%S %Z")
            print(f"[{ts}] Duration not applied yet (widget={actual_value}, EndTime={end_time}, "
                  f"expected {kendo_value} min / {expected_end}) — retrying ({set_attempt + 1}/5)...")
            page.wait_for_timeout(500)

        print(f"Duration={duration_minutes} min (kendo={kendo_value})  EndTime={end_time}  "
              f"expected={expected_end}  AvailableOptions={duration_options}")

        if not duration_ok:
            print(f"Could not confirm duration {kendo_value} min (EndTime={end_time}, expected {expected_end}) — "
                  f"aborting before Save to avoid booking the wrong length.")
            return False
    else:
        print("Duration=any — using CourtReserve default")

    if dry_run:
        print("DRY RUN — stopping before Save. Modal is open; close the browser to exit.")
        page.wait_for_timeout(8000)
        return False

    # Court picker handling — ONLY for orgs with one (e.g. Santa Clara). After the
    # duration is set, CourtReserve repopulates #CourtId *asynchronously* with only the
    # courts free for the ENTIRE window, but it does NOT auto-select one — so Save fails
    # with "Please select a court" unless we pick it ourselves. So: wait for the options
    # to load, then select the first available court. An empty dropdown is the real
    # "no court spans the window" signal — keep watching. (The ReadConsolidated feed's
    # per-interval AvailableCourtIds can't be trusted for this; the picker is the truth.)
    # Orgs WITHOUT a picker (e.g. Sunnyvale) leave #CourtId blank even for bookable slots,
    # so this is skipped for them (they rely on slot_has_window + the post-Save backstop).
    if duration_minutes > 0 and _org_uses_court_picker(org):
        selected_court = None
        for _ in range(10):  # up to ~5s for the async court list to populate
            selected_court = page.evaluate("""(function() {
                var el = document.getElementById("CourtId");
                var w = (window.$ && $("#CourtId").data) ? $("#CourtId").data("kendoDropDownList") : null;
                function ne(v) { return v !== undefined && v !== null && String(v).trim() !== ""; }
                // Already selected (auto-fill or a prior pass)? keep it.
                if (el && ne(el.value)) return String(el.value);
                if (w && ne(w.value())) return String(w.value());
                // Otherwise select the first real option from the widget's dataSource...
                if (w && w.dataSource) {
                    var vf = w.options.dataValueField, d = w.dataSource.data();
                    for (var i = 0; i < d.length; i++) {
                        if (ne(d[i][vf])) { w.value(String(d[i][vf])); w.trigger("change"); return String(d[i][vf]); }
                    }
                }
                // ...or from a plain <select> as a fallback.
                if (el && el.options) {
                    for (var j = 0; j < el.options.length; j++) {
                        if (ne(el.options[j].value)) {
                            el.value = el.options[j].value;
                            el.dispatchEvent(new Event("change", {bubbles: true}));
                            return el.value;
                        }
                    }
                }
                return null;
            })()""")
            if selected_court:
                break
            page.wait_for_timeout(500)
        if not selected_court:
            ts = datetime.now(pytz.timezone("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M:%S %Z")
            msg = (f"No court is available for the full {duration_minutes}-min window at "
                   f"{slot.start_time} (court picker empty) — keep watching.")
            print(f"[{ts}] {msg}")
            raise CourtSelectionRequiredError(msg)
        print(f"Selected court {selected_court} for the full {duration_minutes}-min window.")
        page.wait_for_timeout(300)

    # Accept the disclosure checkbox
    page.evaluate("""(function() {
        var cb = document.getElementById("DisclosureAgree");
        if (cb && !cb.checked) { cb.checked = true; cb.dispatchEvent(new Event("change", {bubbles: true})); }
    })()""")
    page.wait_for_timeout(300)

    # Capture the new ReservationId straight from the save response — it's the most
    # reliable source (the after-the-fact bookings list can lag or filter it out, which
    # left transfers stuck with no id to cancel). Best-effort: scan any reservation-ish
    # response body for an id field.
    _save_capture: dict = {}

    def _on_save_response(resp):
        if _save_capture.get("id"):
            return
        if not _may_carry_new_reservation_id(resp.url):
            return
        try:
            body = resp.text()
        except Exception:
            return
        # Only trust the specific ReservationId field — a generic "Id" could belong to
        # something else and we must not cancel the wrong reservation during a transfer.
        m = re.search(r'"[Rr]eservationId"\s*:\s*"?(\d{3,})', body or "")
        if m:
            _save_capture["id"] = m.group(1)

    page.on("response", _on_save_response)
    try:
        save_btn.click()
        page.wait_for_timeout(4000)
    finally:
        page.remove_listener("response", _on_save_response)

    if result is not None and _save_capture.get("id"):
        result["reservation_id"] = _save_capture["id"]
        print(f"Captured reservation id from save response: {_save_capture['id']}")

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
    if "reservation confirmed" in page_text.lower():
        print("Booking confirmed!")
        return True
    for marker in ["is only allowed", "not allowed", "cannot reserve", "restricted to", "no available courts", "select a court"]:
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

    print("Could not confirm booking (no confirmation message found) — treating as failed.")
    return False


def get_my_reservations(page: Page, org: OrgConfig) -> list[dict]:
    """Fetch upcoming reservations for the logged-in account from CourtReserve. Raises
    ReservationListError when the list can't be read; [] means it was read and empty."""
    captured = {}

    def _on_response(resp):
        if "my-bookings-portal/get-list" in resp.url:
            try:
                captured["body"] = resp.text()
            except Exception:
                pass

    page.on("response", _on_response)
    try:
        page.goto(
            f"https://app.courtreserve.com/Online/Bookings/List/{org.org_id}?type=1",
            wait_until="networkidle",
            timeout=30000,
        )
        page.wait_for_timeout(2000)
    finally:
        page.remove_listener("response", _on_response)

    raw_body = captured.get("body")
    if not raw_body:
        raise ReservationListError("the reservation list didn't load (no get-list response)")

    try:
        payload = json.loads(raw_body)
    except ValueError as exc:
        raise ReservationListError(f"the reservation list wasn't JSON: {exc}") from exc
    items = payload.get("Data") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        # "Data": [] is a real empty list; a missing or odd "Data" is not.
        raise ReservationListError("the reservation list wasn't in the expected shape")

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


def match_reservation_id(reservations: list[dict], slot_date: str, slot_time: str) -> Optional[str]:
    """From get_my_reservations() output, return the reservation_id whose date and
    start_time match the given slot (YYYY-MM-DD / HH:MM), or None. Used to locate the
    reservation a probe account just made so it can be cancelled for transfer."""
    for r in reservations:
        if r.get("date") == slot_date and r.get("start_time") == slot_time:
            return r.get("reservation_id") or None
    return None


def cancel_reservation(page: Page, reservation_id: str, org: OrgConfig = DEFAULT_ORG_CONFIG,
                       before_first_click=None) -> bool:
    """Cancel one reservation through its detail page. `before_first_click`, if given,
    is called once the page has loaded and before anything on it is clicked: an error
    before it means nothing was done on CourtReserve. From the first click on, a page
    that isn't what this code expects (a changed cancel dialog, a slow update) can't
    tell "not cancelled" from "cancelled", so callers must treat any failure after it
    as "may have gone through"."""
    reason = random.choice(CANCEL_REASONS)
    detail_url = f"https://app.courtreserve.com/Online/MyProfile/Reservation/{org.org_id}/{reservation_id}"
    print(f"Cancelling reservation {reservation_id} (reason: '{reason}')...")

    page.goto(detail_url, wait_until="domcontentloaded", timeout=20000)
    kind = _page_kind(page)

    # Everything that can refuse without touching CourtReserve does so here, before the
    # hook: a raise from here on means nothing was clicked and the probe keeps the court.
    if "login" in page.url.lower():
        raise BookingError("the reservation page redirected to the login page (session "
                           "expired?). Nothing was clicked.")
    if kind == "portal":
        # The portal's cancel flow hasn't been seen yet (no account held a reservation
        # to look at), and the form below is gone from it: #39 clicked "Cancel
        # Reservation" there and the reason field never came. Don't click into it blind.
        raise PageChangedError(
            "page changed: the reservation page is CourtReserve's new portal (data-testid="
            "reservation-details-page); its cancel dialog isn't mapped yet and the old form "
            "(#SelectedReservation_CancellationReason) is gone. Nothing was clicked.")
    trigger = page.get_by_text("Cancel Reservation", exact=True)
    if trigger.count() == 0:
        raise PageChangedError("page changed: no 'Cancel Reservation' button on the "
                               "reservation page. Nothing was clicked.")

    if before_first_click:
        before_first_click()
    trigger.first.click()
    page.wait_for_timeout(2000)

    # Fill cancellation reason (required)
    try:
        reason_field = page.wait_for_selector("#SelectedReservation_CancellationReason", timeout=5000)
    except Exception:
        raise PageChangedError(
            "page changed: the cancel dialog has no cancellation-reason field "
            "(#SelectedReservation_CancellationReason) after 'Cancel Reservation' was clicked") from None
    reason_field.fill(reason)
    page.wait_for_timeout(300)

    submit = page.query_selector('button[type="submit"]:has-text("Cancel Reservation")')
    if submit is None:
        raise PageChangedError("page changed: the cancel dialog has no 'Cancel Reservation' "
                               "submit button")
    submit.click()
    page.wait_for_timeout(4000)

    if _cancel_confirmed(page, detail_url):
        print("Cancellation confirmed.")
        return True
    print("Cancellation not confirmed: the reservation page doesn't show it as cancelled.")
    return False


# The reservation's own status on its page: the portal's [data-testid=status] (seen live
# on a cancelled reservation: "Canceled"), else the old page's body text.
_RESERVATION_STATUS_JS = """(function() {
    var s = document.querySelector("[data-testid=status]");
    return s ? {portal: true, text: s.innerText || ""}
             : {portal: false, text: document.body ? document.body.innerText || "" : ""};
})()"""


def _cancel_confirmed(page: Page, detail_url: str) -> bool:
    """Whether the reservation page, loaded fresh, says the reservation is cancelled.

    A positive answer from the reservation itself, not "we left the detail page": a
    redirect to the login page (an expired session) used to read as a cancel while the
    probe still held the court."""
    page.goto(detail_url, wait_until="domcontentloaded", timeout=20000)
    kind = _page_kind(page)
    if "login" in page.url.lower():
        return False
    if kind == "portal":
        # The portal's shell is there before React renders the reservation into it.
        try:
            page.wait_for_selector("[data-testid=status]", timeout=10000)
        except Exception:
            return False
    status = page.evaluate(_RESERVATION_STATUS_JS) or {}
    text = (status.get("text") or "").strip().lower()
    if status.get("portal"):
        return text in ("canceled", "cancelled")
    return "cancelled" in text or "canceled" in text
