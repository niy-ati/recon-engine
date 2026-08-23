"""
Tests for extras/smart_collect_reconcile.py.
"""
import importlib.util
import sys
import unittest
from pathlib import Path

EXTRAS = Path(__file__).resolve().parent.parent / "extras"
SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

spec = importlib.util.spec_from_file_location("smart_collect_reconcile", EXTRAS / "smart_collect_reconcile.py")
smart_collect = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smart_collect)


class TestSmartCollectReconcile(unittest.TestCase):
    def test_every_credit_gets_a_result(self):
        results = smart_collect.reconcile_virtual_account()
        self.assertEqual(len(results), len(smart_collect.credits))
        self.assertTrue(all(r["status"] is not None for r in results))

    def test_trap_case_never_falsely_matched(self):
        """Two payers, identical amount, no other signal -- neither may
        land on a confident MATCHED status."""
        results = smart_collect.reconcile_virtual_account()
        trap = [r for r in results if r["credit_id"] in ("cr_trapA", "cr_trapB")]
        self.assertEqual(len(trap), 2)
        for r in trap:
            self.assertIn(r["status"], ("EXCEPTION", "MATCHED_LOW_CONFIDENCE"))

    def test_gate_reused_is_the_real_validation_gate_module(self):
        """Confirms this script imports the same resolve_with_gate
        reconcile.py is restricted to, not a local reimplementation."""
        import validation_gate
        self.assertIs(smart_collect.resolve_with_gate, validation_gate.resolve_with_gate)


if __name__ == "__main__":
    unittest.main()
