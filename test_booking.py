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
    match_reservation_id,
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


if __name__ == "__main__":
    unittest.main()
