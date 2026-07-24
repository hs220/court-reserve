"""Unit tests for the pure helpers in web/job_runner.py.

Importing web.job_runner creates a SQLite engine (via web.database), so we point
DATA_DIR at a throwaway temp dir *before* importing to avoid touching real data.

Run:  python -m unittest test_job_runner -v
"""

import json
import os
import tempfile
import unittest

# Must be set before importing web.job_runner -> web.database (reads DATA_DIR at import).
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cr-test-data-"))

from web.job_runner import _is_network_error, _parse_preferred_times


class IsNetworkErrorTests(unittest.TestCase):
    def test_timeout_is_transient(self):
        self.assertTrue(_is_network_error(Exception("Page.wait_for_selector: Timeout 15000ms exceeded")))

    def test_dns_and_connection_errors_are_transient(self):
        self.assertTrue(_is_network_error(Exception("getaddrinfo ENOTFOUND")))
        self.assertTrue(_is_network_error(Exception("net::ERR_CONNECTION_REFUSED")))
        self.assertTrue(_is_network_error(Exception("net::ERR_NAME_NOT_RESOLVED")))

    def test_case_insensitive(self):
        self.assertTrue(_is_network_error(Exception("TIMED OUT")))

    def test_socket_hang_up_is_transient(self):
        # Run #260: connection dropped mid-request to ReadConsolidated.
        self.assertTrue(_is_network_error(Exception("APIRequestContext.post: socket hang up")))

    def test_connection_reset_and_bad_gateway_are_transient(self):
        self.assertTrue(_is_network_error(Exception("net::ERR_CONNECTION_RESET")))
        self.assertTrue(_is_network_error(Exception("502 Bad Gateway")))
        self.assertTrue(_is_network_error(Exception("503 Service Unavailable")))

    def test_json_decode_error_is_transient(self):
        # Run #264: ReadConsolidated returned an HTML/Cloudflare page instead of JSON,
        # so APIResponse.json() raised json.JSONDecodeError ("Expecting value: ...").
        try:
            json.loads("<html>error</html>")
        except json.JSONDecodeError as e:
            self.assertTrue(_is_network_error(e))
        else:
            self.fail("expected JSONDecodeError")

    def test_booking_rejection_is_not_transient(self):
        self.assertFalse(_is_network_error(Exception("You are restricted to 1 reservation per day")))

    def test_plain_error_is_not_transient(self):
        self.assertFalse(_is_network_error(ValueError("bad value")))

    def test_real_bug_is_not_transient(self):
        # A genuine parsing bug (missing key) must still fail loudly, not be swallowed.
        self.assertFalse(_is_network_error(KeyError("CourtType")))


class ParsePreferredTimesTests(unittest.TestCase):
    def test_list_is_trimmed_and_emptied(self):
        self.assertEqual(_parse_preferred_times([" 18:00 ", "", "19:00"]), ["18:00", "19:00"])

    def test_comma_separated_string(self):
        self.assertEqual(_parse_preferred_times("18:00, 19:00"), ["18:00", "19:00"])

    def test_single_string(self):
        self.assertEqual(_parse_preferred_times("18:00"), ["18:00"])

    def test_empty_string(self):
        self.assertEqual(_parse_preferred_times(""), [])

    def test_none(self):
        self.assertEqual(_parse_preferred_times(None), [])


if __name__ == "__main__":
    unittest.main()
