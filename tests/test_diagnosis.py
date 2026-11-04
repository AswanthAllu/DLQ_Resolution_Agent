"""tests/test_diagnosis.py — Verify enriched reason-code lookup."""

import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import unittest
from unittest.mock import patch


MOCK_RUNBOOKS = {
    "2085": "https://www.ibm.com/docs/test/2085",
    "2053": "https://www.ibm.com/docs/test/2053",
}


class TestDiagnosis(unittest.TestCase):

    def test_known_code_returns_name(self):
        import diagnosis
        info = diagnosis.lookup(2085)
        self.assertEqual(info["name"], "MQRC_UNKNOWN_OBJECT_NAME")
        self.assertEqual(info["class"], "Configuration")

    def test_unknown_code_returns_generic(self):
        import diagnosis
        info = diagnosis.lookup(9999)
        self.assertIn("9999", info["name"])
        self.assertEqual(info["class"], "Unknown")

    def test_enrich_adds_runbook(self):
        with patch("config_loader.runbooks", return_value=MOCK_RUNBOOKS):
            import diagnosis
            info = diagnosis.enrich(2085)
            self.assertIn("runbook", info)
            self.assertEqual(info["runbook"], "https://www.ibm.com/docs/test/2085")

    def test_enrich_no_runbook_when_not_configured(self):
        with patch("config_loader.runbooks", return_value={}):
            import diagnosis
            info = diagnosis.enrich(2085)
            self.assertNotIn("runbook", info)

    def test_enrich_json_is_valid_json(self):
        import diagnosis
        raw = diagnosis.enrich_json(2053)
        parsed = json.loads(raw)
        self.assertIn("code", parsed)
        self.assertEqual(parsed["code"], 2053)


if __name__ == "__main__":
    unittest.main()
