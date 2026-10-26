"""Unit tests for slot-window validation and slot selection in booking.py.

These guard the fix that prevents the tool from committing to a slot that cannot
hold a single court for the full requested duration (the bug behind the 08:30/120-min
"#main-reservation-container timeout" on Lifetime Sunnyvale).

Run:  python -m unittest test_booking -v
"""

import json
import unittest
from datetime import date, datetime, timedelta

import pytz

from booking import (
    Slot, slot_has_window, find_best_slot,
    _parse_ms, _build_json_data, _classify_booking_error, _org_uses_court_picker,
    match_reservation_id, _feed_json, FeedNotJsonError,
    OrgConfig, BookingError, BookingWindowError,
    NoAvailableCourtsError, AlreadyBookedError, CourtSelectionRequiredError,
)

TZ = "America/Los_Angeles"
BASE = date(2026, 6, 11)
_tz = pytz.timezone(TZ)


def mk(time_str, court_ids, is_wait_list=False, minutes=30):
    """Build a Slot at HH:MM on BASE with the given available court ids."""
    h, m = map(int, time_str.split(":"))
    start = _tz.localize(datetime(BASE.year, BASE.month, BASE.day, h, m))
    end = start + timedelta(minutes=minutes)
    return Slot(
        court_type="Hard",
        start_ms=int(start.timestamp() * 1000),
        end_ms=int(end.timestamp() * 1000),
        available_courts=len(court_ids),
        available_court_ids=list(court_ids),
        is_wait_list=is_wait_list,
        timezone=TZ,
    )


def at(slots, time_str):
    return next(s for s in slots if s.start_time == time_str)


class SlotHasWindowTests(unittest.TestCase):
    def test_no_duration_constraint_is_always_ok(self):
        slots = [mk("08:00", [1])]
        self.assertTrue(slot_has_window(slots, slots[0], 0))
        self.assertTrue(slot_has_window(slots, slots[0], -10))

    def test_single_interval_fits_30_min(self):
        slots = [mk("21:30", [52030, 52031])]
        self.assertTrue(slot_has_window(slots, slots[0], 30))

    def test_single_interval_cannot_fit_60_min(self):
        # 21:30 is free but 22:00 has no availability -> only 30 min bookable.
        slots = [mk("21:30", [52030, 52031])]
        self.assertFalse(slot_has_window(slots, slots[0], 60))

    def test_two_contiguous_intervals_sharing_a_court(self):
        slots = [mk("08:00", [1, 2, 3]), mk("08:30", [2, 3])]
        self.assertTrue(slot_has_window(slots, at(slots, "08:00"), 60))

    def test_two_contiguous_intervals_with_no_common_court(self):
        # Both 30-min intervals are open, but no single court spans both.
        slots = [mk("08:00", [1, 2]), mk("08:30", [3, 4])]
        self.assertFalse(slot_has_window(slots, at(slots, "08:00"), 60))

    def test_gap_in_the_middle_breaks_the_window(self):
        # 09:00 interval is entirely missing from the feed.
        slots = [mk("08:00", [1]), mk("08:30", [1])]  # nothing at 09:00
        self.assertFalse(slot_has_window(slots, at(slots, "08:00"), 90))

    def test_single_court_threads_the_whole_window(self):
        # Santa Clara 08:00-10:00: only court 52102 is free across all four intervals.
        slots = [
            mk("08:00", [52097, 52098, 52099, 52101, 52102, 52103]),
            mk("08:30", [52097, 52098, 52099, 52101, 52102]),
            mk("09:00", [52102]),
            mk("09:30", [52102]),
            mk("10:00", [52102]),
        ]
        self.assertTrue(slot_has_window(slots, at(slots, "08:00"), 120))
        # And starting at 08:30 still works because 52102 is present there too.
        self.assertTrue(slot_has_window(slots, at(slots, "08:30"), 120))

    def test_later_narrowing_to_a_court_absent_from_start(self):
        # Start has many courts, but the tail narrows to a court not free at the start.
        slots = [
            mk("08:00", [1, 2, 3]),
            mk("08:30", [1, 2, 3]),
            mk("09:00", [9]),   # only court 9, which was never free at 08:00
            mk("09:30", [9]),
        ]
        self.assertFalse(slot_has_window(slots, at(slots, "08:00"), 120))

    def test_waitlist_interval_breaks_the_window(self):
        slots = [
            mk("08:00", [1, 2]),
            mk("08:30", [1, 2], is_wait_list=True),
        ]
        self.assertFalse(slot_has_window(slots, at(slots, "08:00"), 60))

    def test_duration_rounds_up_to_interval_count(self):
        # 45 min needs two 30-min intervals; 90 min needs three.
        slots = [mk("08:00", [1]), mk("08:30", [1]), mk("09:00", [1])]
        self.assertTrue(slot_has_window(slots, at(slots, "08:00"), 45))
        self.assertTrue(slot_has_window(slots, at(slots, "08:00"), 90))
        # 91 min would need a fourth interval that doesn't exist.
        self.assertFalse(slot_has_window(slots, at(slots, "08:00"), 91))

    def test_degenerate_zero_length_interval_falls_back_to_30_min(self):
        s = mk("08:00", [1])
        s.end_ms = s.start_ms  # interval == 0 -> code uses _INTERVAL_MS
        slots = [s, mk("08:30", [1])]
        self.assertTrue(slot_has_window(slots, s, 60))


class FindBestSlotTests(unittest.TestCase):
    def _santa_clara(self):
        # Subset of real Santa Clara data: a tight morning block plus an isolated tail.
        return [
            mk("08:00", [52097, 52098, 52099, 52101, 52102, 52103]),
            mk("08:30", [52097, 52098, 52099, 52101, 52102]),
            mk("09:00", [52102]),
            mk("09:30", [52102]),
            mk("10:00", [52102]),
            mk("17:00", [52096, 52097]),  # 17:30+ missing -> isolated
        ]

    def test_empty_slots_returns_none(self):
        self.assertIsNone(find_best_slot([], ["08:00"], duration_minutes=120))

    def test_preferred_time_that_fits_is_returned(self):
        slots = self._santa_clara()
        chosen = find_best_slot(slots, ["08:00"], duration_minutes=120)
        self.assertEqual(chosen.start_time, "08:00")

    def test_preferred_time_that_doesnt_fit_falls_back(self):
        # 17:00 can't hold 120 min; with fallback allowed we get the first slot that can.
        slots = self._santa_clara()
        chosen = find_best_slot(slots, ["17:00"], allow_fallback=True, duration_minutes=120)
        self.assertEqual(chosen.start_time, "08:00")

    def test_preferred_unfit_no_fallback_returns_none(self):
        slots = self._santa_clara()
        self.assertIsNone(
            find_best_slot(slots, ["17:00"], allow_fallback=False, duration_minutes=120)
        )

    def test_nothing_fits_returns_none_when_duration_enforced(self):
        # Only isolated 30-min openings; nothing can hold 120 min.
        slots = [mk("17:00", [1]), mk("21:30", [2])]
        self.assertIsNone(
            find_best_slot(slots, [], allow_fallback=True, duration_minutes=120)
        )

    def test_waitlist_preferred_match_is_skipped(self):
        slots = [mk("08:00", [1, 2], is_wait_list=True), mk("08:30", [1, 2])]
        # 08:00 is waitlist -> not chosen even though it's the preferred time.
        self.assertIsNone(
            find_best_slot(slots, ["08:00"], allow_fallback=False, duration_minutes=30)
        )

    def test_regression_sunnyvale_0830_120min_is_not_booked(self):
        # The exact failing run: 08:30 open for its interval, but 09:00+ unavailable.
        slots = [
            mk("08:00", [52028, 52029, 52035, 52036]),
            mk("08:30", [52035, 52036, 52037, 52047]),
            # nothing at 09:00 / 09:30 / 10:00
            mk("11:30", [52030, 52032]),
        ]
        self.assertIsNone(find_best_slot(slots, ["08:30"], duration_minutes=120))

    # --- backward-compatibility: duration_minutes defaults to 0 (no window check) ---

    def test_legacy_no_duration_returns_preferred_regardless_of_window(self):
        slots = [mk("08:30", [1]), mk("11:30", [2])]  # 08:30 only 30-min free
        chosen = find_best_slot(slots, ["08:30"])  # no duration arg
        self.assertEqual(chosen.start_time, "08:30")

    def test_legacy_last_resort_returns_first_slot(self):
        slots = [mk("08:00", [1], is_wait_list=True), mk("08:30", [2], is_wait_list=True)]
        # No non-waitlist match and no duration constraint -> returns slots[0].
        chosen = find_best_slot(slots, ["99:99"], allow_fallback=True)
        self.assertIs(chosen, slots[0])

    # --- exclude_starts: demote dead slots (no-modal) and re-pick within the list ---

    def test_dead_preferred_falls_through_to_next_preferred(self):
        # Top preference 20:00 is dead (no modal); fall through to the next preferred
        # time, 19:00 — in descending-preference order.
        slots = [mk("20:00", [1, 2]), mk("19:00", [3, 4]), mk("18:00", [5, 6])]
        dead = {at(slots, "20:00").start_ms}
        chosen = find_best_slot(slots, ["20:00", "19:00", "18:00"],
                                allow_fallback=False, exclude_starts=dead)
        self.assertEqual(chosen.start_time, "19:00")

    def test_never_books_outside_preferred_list(self):
        # 20:00 is the only preferred time and it's dead — we must NOT grab 18:00,
        # which isn't on the list. Preferred-only means return None here.
        slots = [mk("20:00", [1, 2]), mk("18:00", [3, 4])]
        dead = {at(slots, "20:00").start_ms}
        self.assertIsNone(
            find_best_slot(slots, ["20:00"], allow_fallback=False, exclude_starts=dead)
        )

    def test_all_preferred_excluded_returns_none(self):
        slots = [mk("08:00", [1, 2]), mk("08:30", [3, 4])]
        dead = {s.start_ms for s in slots}
        self.assertIsNone(
            find_best_slot(slots, ["08:00", "08:30"], allow_fallback=False, exclude_starts=dead)
        )

    def test_exclude_starts_skips_dead_in_last_resort(self):
        # No preferred list -> fallback path; excluded slot is skipped in last resort.
        slots = [mk("08:00", [1], is_wait_list=True), mk("08:30", [2], is_wait_list=True)]
        dead = {at(slots, "08:00").start_ms}
        chosen = find_best_slot(slots, [], allow_fallback=True, exclude_starts=dead)
        self.assertIs(chosen, slots[1])


class ParseMsTests(unittest.TestCase):
    def test_parses_dotnet_date_format(self):
        self.assertEqual(_parse_ms("/Date(1748000000000)/"), 1748000000000)

    def test_parses_plain_value_unchanged(self):
        # Robust to a value that's already stripped of the /Date(...)/ wrapper.
        self.assertEqual(_parse_ms("1748000000000"), 1748000000000)


class BuildJsonDataTests(unittest.TestCase):
    def setUp(self):
        self.org = OrgConfig("13234", "16995", "141206", "America/Los_Angeles")

    def test_summer_date_uses_pdt_offset(self):
        # Noon PDT (UTC-7) on 2026-06-11 -> 19:00 UTC.
        d = json.loads(_build_json_data(date(2026, 6, 11), self.org))
        self.assertEqual(d["startDate"], "2026-06-11T19:00:00.000Z")
        self.assertEqual(d["Date"], "Thu, 11 Jun 2026 19:00:00 GMT")

    def test_winter_date_uses_pst_offset(self):
        # Noon PST (UTC-8) on 2026-01-15 -> 20:00 UTC.
        d = json.loads(_build_json_data(date(2026, 1, 15), self.org))
        self.assertEqual(d["startDate"], "2026-01-15T20:00:00.000Z")

    def test_payload_carries_org_identifiers_and_local_date(self):
        d = json.loads(_build_json_data(date(2026, 6, 11), self.org))
        self.assertEqual(d["orgId"], "13234")
        self.assertEqual(d["CustomSchedulerId"], "16995")
        self.assertEqual(d["CostTypeId"], "141206")
        self.assertEqual(d["TimeZone"], "America/Los_Angeles")
        self.assertEqual(d["KendoDate"], {"Year": 2026, "Month": 6, "Day": 11})


class ClassifyBookingErrorTests(unittest.TestCase):
    def test_advance_window_message(self):
        err = _classify_booking_error("You are only allowed to reserve up to 7 days out")
        self.assertIsInstance(err, BookingWindowError)

    def test_no_available_courts_message(self):
        err = _classify_booking_error("No available courts for this time")
        self.assertIsInstance(err, NoAvailableCourtsError)

    def test_restricted_per_day_message(self):
        err = _classify_booking_error("You are restricted to 1 reservation per day")
        self.assertIsInstance(err, AlreadyBookedError)

    def test_restricted_court_message(self):
        err = _classify_booking_error("Restricted to one court at a time")
        self.assertIsInstance(err, AlreadyBookedError)

    def test_restricted_without_qualifier_is_generic(self):
        # "restricted to" alone (no per-day/court qualifier) -> base BookingError.
        err = _classify_booking_error("Access restricted to members")
        self.assertIsInstance(err, BookingError)
        self.assertNotIsInstance(err, AlreadyBookedError)

    def test_case_insensitive(self):
        err = _classify_booking_error("NO AVAILABLE COURTS")
        self.assertIsInstance(err, NoAvailableCourtsError)

    def test_unknown_message_is_generic(self):
        err = _classify_booking_error("Something weird happened")
        self.assertIsInstance(err, BookingError)
        self.assertNotIsInstance(err, BookingWindowError)

    def test_please_select_a_court_is_not_terminal(self):
        err = _classify_booking_error("Please select a court")
        self.assertIsInstance(err, CourtSelectionRequiredError)
        # Still a BookingError so generic handlers don't break, but distinguishable
        # so the watch/book loops can keep trying instead of failing the job.
        self.assertIsInstance(err, BookingError)
        self.assertNotIsInstance(err, NoAvailableCourtsError)

    def test_court_selection_matches_real_bom_prefixed_notice(self):
        # The exact popup text from run 189 (leading BOM chars + multi-line).
        msg = "﻿﻿Reservation Notice\nPlease select a court\nOK"
        self.assertIsInstance(_classify_booking_error(msg), CourtSelectionRequiredError)

    def test_sunnyvale_no_available_courts_notice(self):
        # Exact Save-rejection popup captured from Lifetime Sunnyvale for a window
        # with no court free the whole time — the signal to keep watching there.
        msg = "﻿﻿Reservation Notice\nSorry, no available courts for the time requested.\nOK"
        self.assertIsInstance(_classify_booking_error(msg), NoAvailableCourtsError)


class MatchReservationIdTests(unittest.TestCase):
    RES = [
        {"reservation_id": "111", "date": "2026-06-07", "start_time": "13:00", "duration_min": 120},
        {"reservation_id": "222", "date": "2026-06-07", "start_time": "08:30", "duration_min": 60},
    ]

    def test_matches_date_and_time(self):
        self.assertEqual(match_reservation_id(self.RES, "2026-06-07", "13:00"), "111")
        self.assertEqual(match_reservation_id(self.RES, "2026-06-07", "08:30"), "222")

    def test_no_match_returns_none(self):
        self.assertIsNone(match_reservation_id(self.RES, "2026-06-07", "09:00"))
        self.assertIsNone(match_reservation_id(self.RES, "2026-06-08", "13:00"))

    def test_empty_list(self):
        self.assertIsNone(match_reservation_id([], "2026-06-07", "13:00"))

    def test_blank_reservation_id_is_none(self):
        res = [{"reservation_id": "", "date": "2026-06-07", "start_time": "13:00"}]
        self.assertIsNone(match_reservation_id(res, "2026-06-07", "13:00"))


class OrgCourtPickerTests(unittest.TestCase):
    def test_santa_clara_uses_court_picker(self):
        org = OrgConfig("13234", "16995", "141206")
        self.assertTrue(_org_uses_court_picker(org))

    def test_sunnyvale_does_not_use_court_picker(self):
        # Sunnyvale leaves #CourtId blank even for bookable slots, so the picker
        # check must be skipped or it would never book.
        org = OrgConfig("13233", "16983", "141205")
        self.assertFalse(_org_uses_court_picker(org))

    def test_unknown_org_defaults_to_no_picker(self):
        org = OrgConfig("99999", "0", "0")
        self.assertFalse(_org_uses_court_picker(org))


class _FakeResponse:
    """Stands in for a Playwright APIResponse whose body isn't the JSON feed."""

    def __init__(self, body, status=200, text_raises=False):
        self.status = status
        self._body = body
        self._text_raises = text_raises

    def json(self):
        return json.loads(self._body)

    def text(self):
        if self._text_raises:
            raise RuntimeError("body already consumed")
        return self._body


WAIVER_PAGE = (
    "\r\n\r\n<!DOCTYPE html><html><head><title>Membership Waiver</title></head>"
    "<body><script>var a=1;</script><h1>Please sign your waiver</h1>"
    "<p>A signed waiver is required before booking.</p></body></html>"
)


class FeedNotJsonTests(unittest.TestCase):
    """Run #331 logged 'Expecting value: line 3 column 1 (char 4)' 1,151 times without
    once saying that the site was asking for a waiver. The body holds the answer."""

    def test_json_body_passes_through(self):
        self.assertEqual(_feed_json(_FakeResponse('{"Data": []}')), {"Data": []})

    def test_html_body_reports_status_title_and_text(self):
        with self.assertRaises(FeedNotJsonError) as cm:
            _feed_json(_FakeResponse(WAIVER_PAGE))
        msg = str(cm.exception)
        self.assertIn("HTTP 200", msg)
        self.assertIn("Membership Waiver", msg)
        self.assertIn("Please sign your waiver", msg)
        self.assertNotIn("var a=1", msg)   # scripts are noise, not content

    def test_it_stays_a_json_decode_error(self):
        # net_errors.is_network_error keys off the type; breaking that would turn every
        # Cloudflare blip into a hard failure.
        from net_errors import is_network_error
        with self.assertRaises(json.JSONDecodeError) as cm:
            _feed_json(_FakeResponse(WAIVER_PAGE))
        self.assertTrue(is_network_error(cm.exception))

    def test_unreadable_body_still_raises_cleanly(self):
        with self.assertRaises(FeedNotJsonError) as cm:
            _feed_json(_FakeResponse(WAIVER_PAGE, status=503, text_raises=True))
        self.assertIn("HTTP 503", str(cm.exception))
        self.assertIn("body unavailable", str(cm.exception))


class CancelAndCaptureTests(unittest.TestCase):
    """What a transfer relies on: knowing whether a cancel was started, and never
    taking another reservation's id for the one just booked."""

    def _page(self, has_reason_field=True, has_submit=True, kind="legacy",
              status=None, has_trigger=True):
        """A fake reservation page. `kind` is what _PAGE_KIND_JS answers; `status` what
        _RESERVATION_STATUS_JS answers once the page is reloaded after the submit."""
        from unittest import mock
        page = mock.MagicMock()
        page.url = "https://app.courtreserve.com/Online/MyProfile/Reservation/1/12345678"

        kinds = list(kind) if isinstance(kind, list) else [kind]

        def evaluate(js, *args):
            if "kendoScheduler" in js:
                return kinds.pop(0) if len(kinds) > 1 else kinds[0]
            if "data-testid=status" in js:
                return status if status is not None else {"portal": False, "text": "Reservation cancelled"}
            return ""

        page.evaluate.side_effect = evaluate
        page.get_by_text.return_value.count.return_value = 1 if has_trigger else 0
        if not has_reason_field:
            page.wait_for_selector.side_effect = TimeoutError("Timeout 5000ms exceeded")
        submit = mock.MagicMock() if has_submit else None
        page.query_selector.return_value = submit
        return page, submit

    def org(self):
        from booking import OrgConfig
        return OrgConfig(org_id="1", scheduler_id="1", cost_type_id="1")

    def test_the_hook_runs_before_the_first_click(self):
        from booking import cancel_reservation
        page, submit = self._page()
        order = []
        page.get_by_text.return_value.first.click.side_effect = lambda: order.append("open")
        submit.click.side_effect = lambda: order.append("submit")
        ok = cancel_reservation(page, "12345678", self.org(),
                                before_first_click=lambda: order.append("hook"))
        self.assertTrue(ok)
        self.assertEqual(order, ["hook", "open", "submit"])

    def test_a_changed_cancel_dialog_fails_after_the_hook(self):
        # Transfer #39, live: "Cancel Reservation" was clicked, then the reason field
        # never appeared. Something was done on CourtReserve, so the hook has run.
        from booking import cancel_reservation
        for kwargs in ({"has_reason_field": False}, {"has_submit": False}):
            page, _ = self._page(**kwargs)
            called = []
            with self.assertRaises(Exception, msg=kwargs):
                cancel_reservation(page, "12345678", self.org(),
                                   before_first_click=lambda: called.append(1))
            self.assertEqual(called, [1], kwargs)

    def test_a_page_that_never_loads_fails_before_the_hook(self):
        from booking import cancel_reservation
        page, _ = self._page()
        page.goto.side_effect = TimeoutError("Timeout 20000ms exceeded")
        called = []
        with self.assertRaises(TimeoutError):
            cancel_reservation(page, "12345678", self.org(), before_first_click=lambda: called.append(1))
        self.assertEqual(called, [])
        page.get_by_text.assert_not_called()

    def _refused_before_the_hook(self, page, exc_type):
        from booking import cancel_reservation
        called = []
        with self.assertRaises(exc_type) as cm:
            cancel_reservation(page, "12345678", self.org(), before_first_click=lambda: called.append(1))
        self.assertEqual(called, [])
        page.get_by_text.return_value.first.click.assert_not_called()
        self.assertIn("Nothing was clicked", str(cm.exception))
        return cm.exception

    def test_the_new_portal_is_refused_before_anything_is_clicked(self):
        # Live, 2026-10-25: the reservation page is the React portal. The old form isn't
        # there, so a transfer must stop here -- probe still holds the court -- rather
        # than click in and end up needs_check, as #39 did.
        from booking import PageChangedError
        exc = self._refused_before_the_hook(self._page(kind="portal")[0], PageChangedError)
        self.assertTrue(str(exc).startswith("page changed:"))

    def test_a_missing_cancel_button_is_refused_before_anything_is_clicked(self):
        from booking import PageChangedError
        self._refused_before_the_hook(self._page(has_trigger=False)[0], PageChangedError)

    def test_a_login_redirect_is_refused_before_anything_is_clicked(self):
        page, _ = self._page()
        page.url = "https://app.courtreserve.com/Online/Account/Login/1?returnUrl=%2FOnline"
        self._refused_before_the_hook(page, BookingError)

    def test_a_missing_reason_field_says_the_page_changed(self):
        from booking import cancel_reservation, PageChangedError
        page, _ = self._page(has_reason_field=False)
        with self.assertRaises(PageChangedError) as cm:
            cancel_reservation(page, "12345678", self.org())
        self.assertIn("cancellation-reason field", str(cm.exception))

    def test_a_cancel_counts_only_when_the_reservation_says_so(self):
        from booking import cancel_reservation
        cases = [
            ({"portal": True, "text": "Canceled"}, True),
            ({"portal": True, "text": "Confirmed"}, False),
            ({"portal": False, "text": "Your reservation has been cancelled."}, True),
            ({"portal": False, "text": "Reservation details"}, False),
        ]
        for status, expected in cases:
            page, _ = self._page(status=status)
            self.assertEqual(cancel_reservation(page, "12345678", self.org()), expected, status)
            # The page is loaded fresh to read the answer, not judged by where the submit went.
            self.assertEqual(page.goto.call_count, 2, status)

    def test_a_portal_page_that_never_renders_its_status_is_not_a_cancel(self):
        # Live: the portal's shell is up before React renders [data-testid=status] into
        # it, so the answer is waited for -- and no answer is no confirmation.
        from booking import cancel_reservation
        page, _ = self._page(kind=["legacy", "portal"], status={"portal": True, "text": "Canceled"})

        def wait_for_selector(sel, **kw):
            if sel == "[data-testid=status]":
                raise TimeoutError("Timeout 10000ms exceeded")
            return page.wait_for_selector.return_value

        page.wait_for_selector.side_effect = wait_for_selector
        self.assertFalse(cancel_reservation(page, "12345678", self.org()))
        page.wait_for_selector.side_effect = None
        page, _ = self._page(kind=["legacy", "portal"], status={"portal": True, "text": "Canceled"})
        self.assertTrue(cancel_reservation(page, "12345678", self.org()))
        page.wait_for_selector.assert_any_call("[data-testid=status]", timeout=10000)

    def test_a_login_page_after_the_submit_is_not_a_cancel(self):
        # Before: any redirect off the detail page counted as cancelled, so an expired
        # session read as "cancelled -> lost" while the probe still held the court.
        from booking import cancel_reservation
        page, submit = self._page()
        login = "https://app.courtreserve.com/Online/Account/Login/1?returnUrl=%2FOnline"
        submit.click.side_effect = lambda: setattr(page, "url", login)
        self.assertFalse(cancel_reservation(page, "12345678", self.org()))

    def test_the_grid_feed_is_never_a_source_of_the_new_id(self):
        from booking import _may_carry_new_reservation_id as ok
        self.assertFalse(ok("https://app.courtreserve.com/Online/Reservations/ReadConsolidated/13234"))
        self.assertTrue(ok("https://reservations.courtreserve.com/Online/ReservationsApi/CreateReservation"))
        self.assertTrue(ok("https://app.courtreserve.com/Online/Bookings/Save"))
        self.assertFalse(ok("https://app.courtreserve.com/Online/Portal/Index"))


class BookingPageTests(unittest.TestCase):
    """book_slot's first step: be on a booking page whose Kendo scheduler is bound."""

    URL = "https://app.courtreserve.com/Online/Reservations/Bookings/13233?sId=16983"

    def _page(self, kinds, url=URL):
        """A fake page whose _PAGE_KIND_JS answers come from `kinds`, in order (the last
        one repeats)."""
        from unittest import mock
        page = mock.MagicMock()
        page.url = url
        answers = list(kinds)

        def evaluate(js, *args):
            if "kendoScheduler" in js and "#react-portal-shell" in js:
                return answers.pop(0) if len(answers) > 1 else answers[0]
            return None

        page.evaluate.side_effect = evaluate
        page.title.return_value = "Lifetime Activities: Sunnyvale | powered by CourtReserve"
        return page

    def test_a_live_scheduler_is_used_without_a_reload(self):
        # The pre-warmed page must not be reloaded at release time.
        from booking import _ensure_kendo_scheduler
        page = self._page(["kendo"])
        _ensure_kendo_scheduler(page, self.URL)
        page.goto.assert_not_called()

    def test_a_page_that_lost_its_scheduler_is_reloaded(self):
        # Run 412: on the booking URL, but `$` was gone. Reload instead of evaluating
        # into a page that can't answer.
        from booking import _ensure_kendo_scheduler
        page = self._page(["unknown", "kendo"])
        _ensure_kendo_scheduler(page, self.URL)
        page.goto.assert_called_once()

    def test_a_scheduler_that_binds_after_jquery_is_waited_for(self):
        # Review of #7: after a reload, jQuery is up a moment before the scheduler is
        # bound, and that moment reads "legacy". It must not fail a page that works a
        # second later.
        from booking import _ensure_kendo_scheduler
        page = self._page(["unknown", "legacy", "legacy", "kendo"])
        _ensure_kendo_scheduler(page, self.URL)
        page.goto.assert_called_once()

    def test_a_page_that_stays_legacy_says_the_page_changed(self):
        from booking import _ensure_kendo_scheduler, PageChangedError
        page = self._page(["legacy"])
        with self.assertRaises(PageChangedError) as cm:
            _ensure_kendo_scheduler(page, self.URL)
        self.assertIn("no Kendo scheduler", str(cm.exception))

    def test_the_page_title_stays_out_of_the_message(self):
        # A "504 Gateway Time-out" title would make net_errors retry a page-changed error.
        from booking import _ensure_kendo_scheduler, PageChangedError
        from net_errors import is_network_error
        page = self._page(["unknown"])
        page.title.return_value = "504 Gateway Time-out"
        with self.assertRaises(PageChangedError) as cm:
            _ensure_kendo_scheduler(page, self.URL)
        self.assertNotIn("504", str(cm.exception))
        self.assertFalse(is_network_error(cm.exception))

    def test_the_new_portal_says_the_page_changed(self):
        from booking import _ensure_kendo_scheduler, PageChangedError
        page = self._page(["portal"])
        with self.assertRaises(PageChangedError) as cm:
            _ensure_kendo_scheduler(page, self.URL)
        self.assertTrue(str(cm.exception).startswith("page changed:"))
        self.assertIn("new portal", str(cm.exception))

    def test_a_page_with_no_scheduler_names_where_it_is(self):
        from booking import _ensure_kendo_scheduler, PageChangedError
        page = self._page(["unknown"])
        with self.assertRaises(PageChangedError) as cm:
            _ensure_kendo_scheduler(page, self.URL)
        self.assertIn("url=" + self.URL, str(cm.exception))

    def test_book_slot_clicks_nothing_on_the_new_portal(self):
        from booking import book_slot, PageChangedError
        page = self._page(["portal"])
        slot = mk("19:00", [1])
        with self.assertRaises(PageChangedError):
            book_slot(page, self.URL, slot, duration_minutes=120)
        page.mouse.click.assert_not_called()

    def test_page_changed_is_not_retried_as_a_network_blip(self):
        # A watch retries "network" errors for 30 minutes; a changed page never clears.
        from net_errors import is_network_error
        from booking import _ensure_kendo_scheduler, cancel_reservation, PageChangedError
        errors = []
        for kinds in (["portal"], ["unknown"]):
            try:
                _ensure_kendo_scheduler(self._page(kinds), self.URL)
            except PageChangedError as e:
                errors.append(e)
        cancel_tests = CancelAndCaptureTests()
        for kw in ({"kind": "portal"}, {"has_trigger": False}, {"has_reason_field": False}, {"has_submit": False}):
            try:
                cancel_reservation(cancel_tests._page(**kw)[0], "12345678", cancel_tests.org())
            except PageChangedError as e:
                errors.append(e)
        self.assertEqual(len(errors), 6)
        for e in errors:
            self.assertFalse(is_network_error(e), str(e))


if __name__ == "__main__":
    unittest.main()
