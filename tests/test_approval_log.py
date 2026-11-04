"""tests/test_approval_log.py — Verify approval record writing and reading."""

import sys, os, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import unittest
from unittest.mock import patch


class TestApprovalLog(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".log", delete=False, mode="w")
        self.tmp.close()
        self.log_path = self.tmp.name

    def tearDown(self):
        os.unlink(self.log_path)

    def _patch(self):
        return patch("config_loader.approvals", return_value={
            "logPath": self.log_path, "requireApprovalForReplay": True
        })

    def test_record_writes_entry(self):
        with self._patch():
            import approval_log
            approval_log.record("operator1", "replay_dry_run", "MSGID001", "test notes")
            with open(self.log_path, encoding="utf-8") as f:
                content = f.read()
            self.assertIn("operator1", content)
            self.assertIn("MSGID001", content)
            self.assertIn("replay_dry_run", content)

    def test_recent_returns_list(self):
        with self._patch():
            import approval_log
            approval_log.record("op1", "action1", "ID1")
            approval_log.record("op2", "action2", "ID2")
            records = approval_log.recent(10)
            self.assertIsInstance(records, list)
            self.assertGreaterEqual(len(records), 1)

    def test_require_approval_default_true(self):
        with self._patch():
            import approval_log
            self.assertTrue(approval_log.require_approval())

    def test_tab_chars_sanitised(self):
        with self._patch():
            import approval_log
            approval_log.record("op\t1", "action", "MSGID\t002", "notes\there")
            with open(self.log_path, encoding="utf-8") as f:
                content = f.read()
            # Should have exactly 4 tabs per line (field separators only)
            for line in content.strip().splitlines():
                self.assertEqual(line.count("\t"), 4)


if __name__ == "__main__":
    unittest.main()
