"""Unit tests for config loading and OrgConfig construction.

Run:  python -m unittest test_config -v
"""

import os
import unittest
from unittest import mock

import config


class LoadConfigTests(unittest.TestCase):
    def test_raises_without_credentials(self):
        # Clear the environment so CR_EMAIL / CR_PASSWORD are absent.
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                config.load_config()

    def test_includes_credentials_from_env(self):
        with mock.patch.dict(os.environ, {"CR_EMAIL": "a@b.com", "CR_PASSWORD": "pw"}):
            cfg = config.load_config()
        self.assertEqual(cfg["email"], "a@b.com")
        self.assertEqual(cfg["password"], "pw")


class DefaultOrgConfigTests(unittest.TestCase):
    def test_coerces_numeric_ids_to_strings(self):
        # YAML may parse unquoted ids as ints; OrgConfig ids must be strings.
        fake = {"org_id": 13234, "scheduler_id": 16995, "cost_type_id": 141206,
                "timezone": "America/Los_Angeles"}
        with mock.patch("config.load_config", return_value=fake):
            oc = config.default_org_config()
        self.assertEqual(oc.org_id, "13234")
        self.assertEqual(oc.scheduler_id, "16995")
        self.assertEqual(oc.cost_type_id, "141206")
        self.assertEqual(oc.timezone, "America/Los_Angeles")

    def test_falls_back_to_sunnyvale_defaults(self):
        with mock.patch("config.load_config", return_value={}):
            oc = config.default_org_config()
        self.assertEqual(oc.org_id, "13233")
        self.assertEqual(oc.scheduler_id, "16983")
        self.assertEqual(oc.cost_type_id, "141205")
        self.assertEqual(oc.timezone, "America/Los_Angeles")


if __name__ == "__main__":
    unittest.main()
