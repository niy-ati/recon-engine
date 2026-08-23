"""
Unit tests for review_server.py's log-entry rendering -- confirms both the
current structured format and pre-migration plain-string entries render
without crashing.
"""
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import unittest  # noqa: E402
import review_server  # noqa: E402


class TestRenderLogEntry(unittest.TestCase):
    def test_structured_entry_renders_pass_action_detail(self):
        entry = {"pass": "1", "action": "matched", "detail": "settlement<->bank matched",
                  "confidence": None, "timestamp": "2026-08-23T00:00:00Z", "correlation_id": "run-1"}
        html = review_server.render_log_entry(entry)
        self.assertIn("pass 1", html)
        self.assertIn("matched", html)
        self.assertIn("settlement", html)

    def test_structured_entry_with_confidence_shows_it(self):
        entry = {"pass": "3/4", "action": "arbiter_picked", "detail": "picked order_1",
                  "confidence": 0.87, "timestamp": "2026-08-23T00:00:00Z", "correlation_id": "run-1"}
        html = review_server.render_log_entry(entry)
        self.assertIn("0.87", html)

    def test_legacy_plain_string_entry_still_renders(self):
        html = review_server.render_log_entry("PASS1: settlement<->bank matched on UTR+amount+date")
        self.assertIn("settlement", html)

    def test_output_is_html_escaped(self):
        entry = {"pass": "1", "action": "matched", "detail": "a <script>alert(1)</script> b",
                  "confidence": None, "timestamp": "x", "correlation_id": "run-1"}
        html = review_server.render_log_entry(entry)
        self.assertNotIn("<script>", html)


if __name__ == "__main__":
    unittest.main()
