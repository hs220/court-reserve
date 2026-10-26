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
    book_slot, Slot, slot_has_window, find_best_slot,
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
    """book_slot's first step: be on a booking page it can drive -- the old Kendo
    scheduler or the new portal's."""

    URL = "https://app.courtreserve.com/Online/Reservations/Bookings/13233?sId=16983"

    def _page(self, kinds, url=URL, portal_renders=True):
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
        if not portal_renders:
            page.wait_for_selector.side_effect = TimeoutError("Timeout 15000ms exceeded")
            page.query_selector.return_value = None
        return page

    def test_a_live_scheduler_is_used_without_a_reload(self):
        # The pre-warmed page must not be reloaded at release time.
        from booking import ensure_booking_page
        for kind in ("kendo", "portal"):
            page = self._page([kind])
            self.assertEqual(ensure_booking_page(page, self.URL), kind)
            page.goto.assert_not_called()

    def test_a_page_that_lost_its_scheduler_is_reloaded(self):
        # Run 412: on the booking URL, but `$` was gone. Reload instead of evaluating
        # into a page that can't answer.
        from booking import ensure_booking_page
        for kinds, kind in ((["unknown", "kendo"], "kendo"), (["unknown", "portal"], "portal")):
            page = self._page(kinds)
            self.assertEqual(ensure_booking_page(page, self.URL), kind)
            page.goto.assert_called_once()

    def test_a_scheduler_that_binds_after_jquery_is_waited_for(self):
        # Review of #7: after a reload, jQuery is up a moment before the scheduler is
        # bound, and that moment reads "legacy". It must not fail a page that works a
        # second later.
        from booking import ensure_booking_page
        page = self._page(["unknown", "legacy", "legacy", "kendo"])
        self.assertEqual(ensure_booking_page(page, self.URL), "kendo")
        page.goto.assert_called_once()

    def test_a_page_that_stays_legacy_says_the_page_changed(self):
        from booking import ensure_booking_page, PageChangedError
        page = self._page(["legacy"])
        with self.assertRaises(PageChangedError) as cm:
            ensure_booking_page(page, self.URL)
        self.assertIn("no scheduler", str(cm.exception))

    def test_the_page_title_stays_out_of_the_message(self):
        # A "504 Gateway Time-out" title would make net_errors retry a page-changed error.
        from booking import ensure_booking_page, PageChangedError
        from net_errors import is_network_error
        page = self._page(["unknown"])
        page.title.return_value = "504 Gateway Time-out"
        with self.assertRaises(PageChangedError) as cm:
            ensure_booking_page(page, self.URL)
        self.assertNotIn("504", str(cm.exception))
        self.assertFalse(is_network_error(cm.exception))

    def test_a_portal_that_never_shows_its_scheduler_says_the_page_changed(self):
        from booking import ensure_booking_page, PageChangedError
        page = self._page(["portal"], url="https://app.courtreserve.com/Online/Portal/Index/13233",
                          portal_renders=False)
        with self.assertRaises(PageChangedError) as cm:
            ensure_booking_page(page, self.URL)
        self.assertTrue(str(cm.exception).startswith("page changed:"))

    def test_a_page_with_no_scheduler_names_where_it_is(self):
        from booking import ensure_booking_page, PageChangedError
        page = self._page(["unknown"])
        with self.assertRaises(PageChangedError) as cm:
            ensure_booking_page(page, self.URL)
        self.assertIn("url=" + self.URL, str(cm.exception))

    def test_page_changed_is_not_retried_as_a_network_blip(self):
        # A watch retries "network" errors for 30 minutes; a changed page never clears.
        from net_errors import is_network_error
        from booking import ensure_booking_page, cancel_reservation, book_slot, PageChangedError
        errors = []
        for kinds, renders in ((["portal"], False), (["unknown"], True)):
            try:
                ensure_booking_page(self._page(kinds, url="about:blank", portal_renders=renders), self.URL)
            except PageChangedError as e:
                errors.append(e)
        cancel_tests = CancelAndCaptureTests()
        for kw in ({"kind": "portal"}, {"has_trigger": False}, {"has_reason_field": False}, {"has_submit": False}):
            try:
                cancel_reservation(cancel_tests._page(**kw)[0], "12345678", cancel_tests.org())
            except PageChangedError as e:
                errors.append(e)
        for kw in ({"columns": ("Clay",)}, {"form_date": "OCT 31 Saturday 2:30 PM – 3:00 PM"},
                   {"combos": 2}, {"durations": ("soon",)}, {"shown": "2026-10-30"}):
            fake = FakePortal(**kw)
            try:
                with fake.patched():
                    book_slot(fake, self.URL, portal_slot(), duration_minutes=120, org=SUNNYVALE)
            except PageChangedError as e:
                errors.append(e)
        self.assertEqual(len(errors), 11)
        for e in errors:
            self.assertFalse(is_network_error(e), str(e))


SUNNYVALE = OrgConfig(org_id="13233", scheduler_id="16983", cost_type_id="1")


def portal_slot(date_str="2026-10-30", time_str="14:30", court_type="Hard"):
    start = _tz.localize(datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M"))
    ms = int(start.timestamp() * 1000)
    return Slot(court_type=court_type, start_ms=ms, end_ms=ms + 30 * 60_000,
                available_courts=12, available_court_ids=[1], is_wait_list=False)


class FakePortal:
    """CourtReserve's portal booking page and its Create Reservation form, as recorded
    live on 2026-10-25 (Sunnyvale, account 3, Fri Oct 30 2:30 PM; nothing confirmed).
    It answers booking.py's JS by identity and records every click, in order."""

    URL = "https://app.courtreserve.com/Online/Reservations/Bookings/13233?sId=16983"
    DURATIONS = ("30 minutes", "1 hour", "1 hour & 30 minutes", "2 hours")

    def __init__(self, shown="10/25/2026", cells=None, columns=("Hard",),
                 form_date="OCT 30 Friday 2:30 PM – {end} Recreational Play - Tennis Hard",
                 combos=1, durations=DURATIONS, applies_duration=True, terms=True,
                 notices=(), books=True, save_id="64000001", listed_id="64000001", form_dates=()):
        self.url = self.URL
        self.shown = shown
        self.cells = {"2:30 PM": "reserve"} if cells is None else cells
        self.columns, self.form_date, self.combos = columns, form_date, combos
        self.durations, self.applies_duration, self.terms = durations, applies_duration, terms
        self.notices, self.books, self.save_id, self.listed_id = notices, books, save_id, listed_id
        self.form_dates = list(form_dates)  # the first forms that open, then form_date
        self.pick = None
        self.minutes = 30
        self.modal = self.listbox = self.confirmed = False
        self.clicks = []
        self.handlers = []
        self.reservations = []

    # -- what booking.py calls -------------------------------------------------------
    def evaluate(self, js, arg=None):
        import booking as b
        if js is b._PAGE_KIND_JS:
            return "portal"
        if js is b._PORTAL_DATE_JS:
            return self.shown
        if js is b._PORTAL_FIND_SLOT_JS:
            label, court_type, token = arg
            if court_type not in self.columns:
                return {"state": "no column", "columns": list(self.columns)}
            state = self.cells.get(label)
            if state is None:
                return None
            if state != "reserve":
                return {"state": state, "text": state.upper()}
            self.pick = f"[data-cr-pick='{token}']"
            return {"state": "reserve", "pick": self.pick}
        if js is b._PORTAL_SLOT_COUNT_JS:
            return 15
        if js is b._PORTAL_FORM_JS:
            if not self.modal:
                return None
            end = (datetime(2026, 10, 30, 14, 30) + timedelta(minutes=self.minutes)).strftime("%-I:%M %p")
            label = next((d for d in self.durations if b._portal_duration_minutes(d) == self.minutes), f"{self.minutes} minutes")
            form_date = self.form_dates.pop(0) if self.form_dates else self.form_date
            return {"form": True, "dateTime": form_date.format(end=end), "combos": self.combos,
                    "duration": label, "durationValue": str(self.minutes), "terms": self.terms}
        if js is b._PORTAL_OPTIONS_JS:
            return list(self.durations) if self.listbox else []
        if js is b._PORTAL_NOTICES_JS:
            return list(self.notices) if self.confirmed else []
        raise AssertionError(f"unexpected evaluate: {js[:60]}")

    def click(self, selector):
        self.clicks.append(selector)
        d = datetime.strptime(self.shown, "%m/%d/%Y")
        step = {'[aria-label="Next day"]': 1, '[aria-label="Previous day"]': -1}[selector]
        self.shown = (d + timedelta(days=step)).strftime("%m/%d/%Y")

    def query_selector(self, selector):
        import booking as b
        if selector == b._PORTAL_SCHEDULER:
            return object()
        if selector == "[data-testid=scheduler-refresh-btn]":
            return _FakeElement(self, "refresh")
        return None

    def locator(self, selector):
        return _FakeLocator(self, selector)

    def get_by_role(self, role, name=None, exact=False):
        assert role == "option", role
        return _FakeElement(self, f"option:{name}")

    def wait_for_selector(self, selector, **kw):
        import booking as b
        if selector == b._PORTAL_FORM and not self.modal:
            raise TimeoutError("Timeout 15000ms exceeded")
        return object()

    def wait_for_timeout(self, ms):
        pass

    def goto(self, url, **kw):
        self.url = url

    def on(self, event, handler):
        self.handlers.append(handler)

    def remove_listener(self, event, handler):
        self.handlers.remove(handler)

    # -- what the clicks do ------------------------------------------------------------
    def press(self, what):
        import booking as b
        self.clicks.append(what)
        if what == "reserve":
            self.modal = True
        elif what == "close":
            self.modal = False
        elif what == "combobox":
            self.listbox = True
        elif what.startswith("option:"):
            self.listbox = False
            if self.applies_duration:
                self.minutes = b._portal_duration_minutes(what[len("option:"):])
        elif what == "confirm":
            self.confirmed = True
            if self.books:
                for h in list(self.handlers):
                    h(_FakeSaveResponse(self.save_id))
                self.reservations.append({"reservation_id": self.listed_id, "date": "2026-10-30",
                                          "start_time": "14:30", "duration_min": self.minutes,
                                          "court_type": "Hard"})

    def patched(self):
        from unittest import mock
        return mock.patch("booking.get_my_reservations", side_effect=lambda page, org: list(self.reservations))


class _FakeLocator:
    def __init__(self, fake, selector):
        self.fake, self.selector = fake, selector

    def nth(self, i):
        return self

    @property
    def first(self):
        return self

    def locator(self, selector):
        return _FakeLocator(self.fake, selector)

    def get_by_text(self, text, exact=False):
        import booking as b
        assert text == b._PORTAL_TERMS, text
        return _FakeElement(self.fake, "terms")

    def get_by_role(self, role, name=None, exact=False):
        assert (role, name) == ("button", "Confirm"), (role, name)
        return _FakeElement(self.fake, "confirm")

    def click(self, timeout=None):
        if self.selector == self.fake.pick:
            self.fake.press("reserve")
        elif self.selector == ".ant-modal-close":
            self.fake.press("close")
        elif self.selector == "button[role=combobox]":
            self.fake.press("combobox")
        else:
            raise AssertionError(f"unexpected click on {self.selector}")


class _FakeElement:
    def __init__(self, fake, what):
        self.fake, self.what = fake, what

    def click(self):
        self.fake.press(self.what)


class _FakeSaveResponse:
    url = "https://app.courtreserve.com/api/v1/portalreservationsapi/Api_Reservation_Create"

    def __init__(self, rid):
        self.rid = rid

    def text(self):
        return json.dumps({"IsValid": True, "Data": {"ReservationId": int(self.rid)}})


class PortalBookingTests(unittest.TestCase):
    """book_slot on the portal, against a fake that mirrors the recorded DOM."""

    def book(self, fake, duration=30, **kw):
        result = {}
        with fake.patched():
            ok = book_slot(fake, FakePortal.URL, portal_slot(), duration_minutes=duration,
                           org=SUNNYVALE, result=result, **kw)
        return ok, result

    def test_a_30_minute_booking_goes_reserve_terms_confirm_and_is_checked_on_the_list(self):
        fake = FakePortal()
        ok, result = self.book(fake)
        self.assertTrue(ok)
        self.assertEqual(result["reservation_id"], "64000001")
        self.assertEqual(fake.clicks, ['[aria-label="Next day"]'] * 5 + ["refresh", "reserve", "terms", "confirm"])

    def test_a_longer_booking_picks_it_from_the_duration_list(self):
        fake = FakePortal()
        ok, _ = self.book(fake, duration=120)
        self.assertTrue(ok)
        self.assertEqual(fake.clicks[-5:], ["reserve", "combobox", "option:2 hours", "terms", "confirm"])
        self.assertEqual(fake.reservations[0]["duration_min"], 120)

    def test_a_duration_that_doesnt_take_is_never_confirmed(self):
        # The Kendo path's rule: never book the wrong length.
        fake = FakePortal(applies_duration=False)
        ok, _ = self.book(fake, duration=120)
        self.assertFalse(ok)
        self.assertNotIn("confirm", fake.clicks)

    def test_a_duration_not_offered_uses_the_nearest(self):
        fake = FakePortal(durations=("30 minutes", "1 hour", "1 hour & 30 minutes"))
        ok, _ = self.book(fake, duration=120)
        self.assertTrue(ok)
        self.assertIn("option:1 hour & 30 minutes", fake.clicks)

    def test_a_date_picker_that_renders_late_is_waited_for(self):
        # Live dry run: the scheduler container was up while its date picker read None.
        fake = FakePortal()
        late = iter([None, None, None])
        real = fake.evaluate

        def evaluate(js, arg=None):
            import booking as b
            if js is b._PORTAL_DATE_JS:
                return next(late, fake.shown)
            return real(js, arg)

        fake.evaluate = evaluate
        self.assertTrue(self.book(fake)[0])

    def test_the_day_is_stepped_back_too(self):
        fake = FakePortal(shown="11/01/2026")
        self.assertTrue(self.book(fake)[0])
        self.assertEqual(fake.clicks[:2], ['[aria-label="Previous day"]'] * 2)

    def test_a_taken_slot_is_demoted_without_a_click(self):
        from booking import SlotNotBookableError
        for state in ("none available", "unavailable"):
            fake = FakePortal(cells={"2:30 PM": state})
            with self.assertRaises(SlotNotBookableError):
                self.book(fake)
            self.assertNotIn("reserve", fake.clicks)

    def test_a_slot_missing_from_the_grid_is_not_booked(self):
        fake = FakePortal(cells={})
        self.assertFalse(self.book(fake)[0])
        self.assertNotIn("reserve", fake.clicks)

    def test_a_form_for_another_slot_is_never_confirmed(self):
        from booking import PageChangedError
        fake = FakePortal(form_date="OCT 30 Friday 3:00 PM – 3:30 PM Recreational Play - Tennis Hard")
        with self.assertRaises(PageChangedError):
            self.book(fake)
        self.assertNotIn("confirm", fake.clicks)

    def test_a_form_for_the_next_half_hour_is_closed_and_the_slot_retried(self):
        # Live dry run: the grid re-rendered between finding the cell and clicking it,
        # and the 3:00 PM form opened for a 2:30 PM slot.
        fake = FakePortal(form_dates=["OCT 30 Friday 3:00 PM – 3:30 PM Recreational Play - Tennis Hard"])
        self.assertTrue(self.book(fake)[0])
        self.assertEqual(fake.clicks[-5:], ["reserve", "close", "reserve", "terms", "confirm"])

    def test_an_unmapped_second_picker_is_never_confirmed(self):
        # A court picker (the old Santa Clara form had one) hasn't been seen on the portal.
        from booking import PageChangedError
        fake = FakePortal(combos=2)
        with self.assertRaises(PageChangedError):
            self.book(fake)
        self.assertNotIn("confirm", fake.clicks)

    def test_dry_run_stops_before_confirm(self):
        fake = FakePortal()
        self.assertFalse(self.book(fake, dry_run=True)[0])
        self.assertNotIn("confirm", fake.clicks)
        self.assertNotIn("terms", fake.clicks)

    def test_a_confirm_that_isnt_on_the_list_is_not_a_booking(self):
        fake = FakePortal(books=False)
        ok, result = self.book(fake)
        self.assertFalse(ok)
        self.assertNotIn("reservation_id", result)

    def test_the_list_names_the_reservation_not_the_save_response(self):
        # A transfer cancels whatever id is stored: take it from the account's own list.
        fake = FakePortal(save_id="99999999", listed_id="64000002")
        self.assertEqual(self.book(fake)[1]["reservation_id"], "64000002")

    def test_a_refusal_notice_is_classified(self):
        fake = FakePortal(books=False, notices=("You are restricted to 1 court reservation per day.",))
        with self.assertRaises(AlreadyBookedError):
            self.book(fake)

    def test_an_unreadable_list_after_confirm_fails_loudly(self):
        from unittest import mock
        from booking import ReservationListError
        fake = FakePortal()
        with mock.patch("booking.get_my_reservations", side_effect=ReservationListError("no get-list response")):
            with self.assertRaises(ReservationListError) as cm:
                book_slot(fake, FakePortal.URL, portal_slot(), duration_minutes=30, org=SUNNYVALE)
        self.assertIn("Confirm was clicked", str(cm.exception))

    def test_terms_are_skipped_when_the_form_has_none(self):
        fake = FakePortal(terms=False)
        self.assertTrue(self.book(fake)[0])
        self.assertNotIn("terms", fake.clicks)

    def test_duration_wording(self):
        from booking import _portal_duration_label, _portal_duration_minutes
        for minutes, label in ((30, "30 minutes"), (60, "1 hour"), (90, "1 hour & 30 minutes"), (120, "2 hours")):
            self.assertEqual(_portal_duration_label(minutes), label)
            self.assertEqual(_portal_duration_minutes(label), minutes)
        self.assertIsNone(_portal_duration_minutes("soon"))


if __name__ == "__main__":
    unittest.main()
