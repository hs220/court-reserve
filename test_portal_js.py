"""booking.py's portal JavaScript, run in a real headless Chromium against synthetic pages
shaped like the DOM recorded live (Oct 25-27, 2026). Offline: pages are set with
set_content and every network request is aborted.

FakePortal (test_booking) answers this JS by identity, so it can't catch a geometry,
column or label-search mistake -- review of #9 found a column fallback that marked a
Pickleball cell for Hard that way. These tests run the JS itself.

Skipped when Playwright's Chromium can't be launched. CR_TEST_CHROMIUM may name a
chromium executable when the installed build doesn't match the Playwright version.

Run:  python -m unittest test_portal_js -v
"""

import os
import unittest

import booking as b

ROWS = ["12:30 PM", "1:00 PM", "1:30 PM", "2:00 PM", "2:30 PM", "3:00 PM"]
CELL_H, COL_W, LEFT, TOP = 46, 300, 100, 40


def grid(columns, rows, reserve, extra=""):
    """A scheduler like the portal's: a header per court type, a label column, and per
    court type a column of 46px cells -- "Reserve" cells carry the testid, taken ones
    read "None Available"."""
    heads = "".join(
        f'<div data-testid="scheduler-header-court-type-{c}" style="position:absolute;'
        f'left:{LEFT + i * COL_W}px;top:0;width:{COL_W}px;height:{TOP}px">'
        f'<span data-testid="scheduler-header-court-type-name-{c}">{c}</span></div>'
        for i, c in enumerate(columns))
    labels = "".join(
        f'<div style="position:absolute;left:0;top:{TOP + j * CELL_H}px;width:{LEFT}px;'
        f'height:{CELL_H}px">{t}</div>' for j, t in enumerate(rows))
    cells = ""
    for i, c in enumerate(columns):
        for j, t in enumerate(rows):
            body = ('role="button" data-testid="consolidated-reserve-slot">Reserve'
                    if (c, t) in reserve else '>None Available')
            cells += (f'<div style="position:absolute;left:{LEFT + i * COL_W}px;'
                      f'top:{TOP + j * CELL_H}px;width:{COL_W}px;height:{CELL_H}px" {body}</div>')
    return (f'<body style="margin:0"><div data-testid="scheduler-page" style="position:relative;'
            f'height:{TOP + 20 + CELL_H * len(rows)}px">{heads}{labels}{cells}</div>{extra}</body>')


def modal(tile_lines, terms_html, total="$7.00", summary="Player Name $7.00 Subtotal $7.00 Total $7.00"):
    tile = "".join(f"<div>{line}</div>" for line in tile_lines)
    return f"""<body><div class="ant-modal" role="dialog">
      <div data-testid="reservation-form">
        <div data-testid="reservation-date-time-display">{tile}</div>
        <button type="button" role="combobox">30 minutes</button>
        <input aria-hidden="true" name="duration" value="30">
        <div data-testid="reservation-cost-summary">{summary}</div>
        {terms_html}
        <div data-testid="reservation-mobile-total-due">{total}</div>
        <button type="button">Close</button><button type="button">Confirm</button>
      </div></div></body>"""


NESTED_TERMS = ('<div class="ant-flex"><div class="box"><span aria-hidden="true"></span></div>'
                '<span><span><span>I agree to the terms above</span></span></span></div>')
TILE = ["OCT", "30", "Friday", "2:30 PM – 3:00 PM", "Recreational Play - Tennis", "Hard"]


class PortalJsTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
            cls._pw = sync_playwright().start()
            kw = {"executable_path": os.environ["CR_TEST_CHROMIUM"]} if os.environ.get("CR_TEST_CHROMIUM") else {}
            cls._browser = cls._pw.chromium.launch(headless=True, **kw)
        except Exception as exc:  # no browser here: these tests can't run
            if getattr(cls, "_pw", None):
                cls._pw.stop()
            raise unittest.SkipTest(f"Chromium unavailable: {exc}".splitlines()[0])
        cls.page = cls._browser.new_page(viewport={"width": 1280, "height": 900})
        cls.page.route("**/*", lambda route: route.abort())  # nothing leaves this machine

    @classmethod
    def tearDownClass(cls):
        cls._browser.close()
        cls._pw.stop()

    def find(self, html, label="2:30 PM", court="Hard"):
        self.page.set_content(html)
        found = self.page.evaluate(b._PORTAL_FIND_SLOT_JS, [label, court, "t1"])
        picked = self.page.evaluate("""() => { const e = document.querySelector("[data-cr-pick]");
            if (!e) return null; const r = e.getBoundingClientRect();
            return [Math.round(r.left), Math.round(r.top + window.scrollY)]; }""")
        return found, picked

    # -- _PORTAL_FIND_SLOT_JS ---------------------------------------------------------
    def test_the_cell_under_the_court_types_column_at_the_labels_row_is_marked(self):
        found, picked = self.find(grid(["Hard", "Pickleball"], ROWS,
                                       {("Hard", "2:30 PM"), ("Pickleball", "2:30 PM"), ("Hard", "12:30 PM")}))
        self.assertEqual(found, {"state": "reserve", "pick": "[data-cr-pick='t1']"})
        self.assertEqual(picked, [LEFT, TOP + 4 * CELL_H])  # Hard, the 2:30 row

    def test_no_column_for_the_court_type_marks_nothing(self):
        # The review's probe: Hard asked, only Pickleball there. A fallback marked it.
        found, picked = self.find(grid(["Pickleball"], ROWS, {("Pickleball", "2:30 PM")}))
        self.assertEqual(found["state"], "no column")
        self.assertEqual(found["columns"], ["Pickleball"])
        self.assertIsNone(picked)

    def test_a_taken_cell_is_read_as_taken_even_when_the_next_column_is_free(self):
        found, picked = self.find(grid(["Hard", "Pickleball"], ROWS,
                                       {("Pickleball", "2:30 PM"), ("Hard", "3:00 PM")}))
        self.assertEqual(found["state"], "none available")
        self.assertIsNone(picked)

    def test_12_30_is_not_2_30(self):
        found, picked = self.find(grid(["Hard"], ["12:30 PM", "1:00 PM"], {("Hard", "12:30 PM")}))
        self.assertIsNone(found)  # no 2:30 label yet: the grid may still be loading
        self.assertIsNone(picked)

    def test_a_row_below_the_fold_is_scrolled_to(self):
        rows = [f"{h}:{m} AM" for h in range(8, 12) for m in ("00", "30")] + ROWS
        found, picked = self.find(grid(["Hard"], rows, {("Hard", "2:30 PM")}))
        self.assertEqual(found["state"], "reserve")
        self.assertEqual(picked, [LEFT, TOP + rows.index("2:30 PM") * CELL_H])

    def test_a_label_twice_is_ambiguous(self):
        # A second leaf reading "2:30 PM" inside the scheduler: which row is meant?
        html = grid(["Hard"], ROWS, {("Hard", "2:30 PM")}).replace(
            f">{ROWS[0]}</div>", f">{ROWS[0]}</div><div>2:30 PM</div>", 1)
        found, picked = self.find(html)
        self.assertEqual(found["state"], "ambiguous label")
        self.assertIsNone(picked)

    # -- _PORTAL_MARK_TERMS_JS ----------------------------------------------------------
    def test_the_nested_terms_label_marks_only_its_innermost_span(self):
        # Live (book_once, Oct 27): three nested spans with the same text.
        self.page.set_content(modal(TILE, NESTED_TERMS))
        self.assertEqual(self.page.evaluate(b._PORTAL_MARK_TERMS_JS), 1)
        marked = self.page.locator(b._PORTAL_TERMS_MARK)
        self.assertEqual(marked.count(), 1)
        self.assertEqual(marked.evaluate("e => e.querySelectorAll('span').length"), 0)

    def test_two_separate_terms_labels_are_counted(self):
        self.page.set_content(modal(TILE, NESTED_TERMS + NESTED_TERMS))
        self.assertEqual(self.page.evaluate(b._PORTAL_MARK_TERMS_JS), 2)
        self.assertEqual(self.page.locator(b._PORTAL_TERMS_MARK).count(), 0)

    # -- _PORTAL_FORM_JS + _parse_portal_tile -----------------------------------------
    def test_the_form_reads_its_tile_lines_amounts_and_duration(self):
        self.page.set_content(modal(TILE, NESTED_TERMS))
        form = self.page.evaluate(b._PORTAL_FORM_JS)
        self.assertEqual(form["tileLines"], TILE)
        self.assertEqual(b._parse_portal_tile(form["tileLines"])["end"], "3:00 PM")
        self.assertEqual((form["combos"], form["duration"], form["durationValue"]), (1, "30 minutes", "30"))
        self.assertEqual(form["totalDue"], ["$7.00"])  # the amount, never the member's name
        self.assertTrue(form["terms"])

    def test_a_12_30_tile_parses_as_12_30(self):
        self.page.set_content(modal(["OCT", "30", "Friday", "12:30 PM – 1:00 PM",
                                     "Recreational Play - Tennis", "Hard"], NESTED_TERMS))
        tile = b._parse_portal_tile(self.page.evaluate(b._PORTAL_FORM_JS)["tileLines"])
        self.assertEqual(tile["start"], "12:30 PM")


if __name__ == "__main__":
    unittest.main()
