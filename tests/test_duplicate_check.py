"""tests/test_duplicate_check.py — Verify duplicate detection logic."""

import sys, os, tempfile, sqlite3
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import unittest
from unittest.mock import patch
from pathlib import Path


class TestDuplicateCheck(unittest.TestCase):

    def setUp(self):
        # Use a temporary DB for each test
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name

    def tearDown(self):
        os.unlink(self.db_path)

    def _patch(self):
        return patch("config_loader.duplicate_check", return_value={
            "dbPath": self.db_path, "tableName": "processed_messages"
        })

    def test_first_check_is_unique(self):
        with self._patch():
            import duplicate_check
            result = duplicate_check.verify("AABBCCDD")
            self.assertEqual(result, "unique")

    def test_second_check_is_duplicate(self):
        with self._patch():
            import duplicate_check
            duplicate_check.verify("AABBCCDD")
            result = duplicate_check.verify("AABBCCDD")
            self.assertEqual(result, "duplicate")

    def test_empty_id_is_inconclusive(self):
        with self._patch():
            import duplicate_check
            self.assertEqual(duplicate_check.verify(""), "inconclusive")
            self.assertEqual(duplicate_check.verify("   "), "inconclusive")

    def test_mark_processed(self):
        with self._patch():
            import duplicate_check
            ok = duplicate_check.mark_processed("XXYYZZ", "operator1")
            self.assertTrue(ok)
            result = duplicate_check.verify("XXYYZZ")
            self.assertEqual(result, "duplicate")


if __name__ == "__main__":
    unittest.main()
