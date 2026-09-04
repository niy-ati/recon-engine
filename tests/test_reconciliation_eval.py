"""
Tests reconciliation_eval.py's scoring logic directly against small, hand-
built fixtures -- never against a live reconcile() run, so this suite
never depends on generate_data.py's actual seeded batch or a warm Ollama.
"""
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import unittest  # noqa: E402
from reconciliation_eval import canonical_outcome, score, render_report  # noqa: E402


def gt(match_key, expected="", group=""):
    return {"match_key": match_key, "expected": expected, "group": group}


class TestCanonicalOutcome(unittest.TestCase):
    def test_category_wins_when_present(self):
        self.assertEqual(canonical_outcome({"category": "DUPLICATE", "status": "EXCEPTION"}), "DUPLICATE")

    def test_falls_back_to_status_when_no_category(self):
        self.assertEqual(canonical_outcome({"category": None, "status": "MATCHED"}), "MATCHED")


class TestScoreSingleRows(unittest.TestCase):
    def test_correct_row_counted(self):
        results = [{"match_key": "settlement:s1", "category": None, "status": "MATCHED"}]
        report = score(results, [gt("settlement:s1", "MATCHED")])
        self.assertEqual(report["single_rows"]["correct"], 1)
        self.assertEqual(report["overall_accuracy_pct"], 100.0)

    def test_wrong_row_counted(self):
        results = [{"match_key": "settlement:s1", "category": "UNEXPLAINED", "status": "EXCEPTION"}]
        report = score(results, [gt("settlement:s1", "UTR_LEVEL_MISMATCH")])
        self.assertEqual(report["single_rows"]["correct"], 0)
        self.assertEqual(report["overall_accuracy_pct"], 0.0)

    def test_pipe_joined_expected_set_accepts_any_member(self):
        results = [{"match_key": "settlement:s1", "category": "FUZZY_MATCH_NEEDS_REVIEW", "status": "MATCHED_LOW_CONFIDENCE"}]
        report = score(results, [gt("settlement:s1", "MATCHED_AI_ASSISTED|FUZZY_MATCH_NEEDS_REVIEW")])
        self.assertEqual(report["single_rows"]["correct"], 1)

    def test_missing_match_key_flagged_not_silently_dropped(self):
        report = score([], [gt("settlement:ghost", "MATCHED")])
        self.assertIn("settlement:ghost", report["missing_from_results"])
        self.assertEqual(report["single_rows"]["correct"], 0)


class TestScoreDuplicatePairs(unittest.TestCase):
    def test_correct_pair_one_matched_one_duplicate(self):
        results = [
            {"match_key": "settlement:s1", "category": None, "status": "MATCHED"},
            {"match_key": "settlement:s1_dup", "category": "DUPLICATE", "status": "EXCEPTION"},
        ]
        report = score(results, [gt("settlement:s1", group="dup_0"), gt("settlement:s1_dup", group="dup_0")])
        self.assertEqual(report["duplicate_pairs"]["correct"], 1)
        self.assertEqual(report["overall_accuracy_pct"], 100.0)

    def test_pair_wrong_when_both_land_the_same(self):
        """The real failure mode this pairwise check exists to catch: two
        independent per-row set checks would both pass if each row's
        allowed set were {MATCHED, DUPLICATE} -- this must not."""
        results = [
            {"match_key": "settlement:s1", "category": None, "status": "MATCHED"},
            {"match_key": "settlement:s1_dup", "category": None, "status": "MATCHED"},
        ]
        report = score(results, [gt("settlement:s1", group="dup_0"), gt("settlement:s1_dup", group="dup_0")])
        self.assertEqual(report["duplicate_pairs"]["correct"], 0)

    def test_pair_order_independent(self):
        """Which twin wins is legitimately nondeterministic (shuffled file
        order) -- the pair is correct either way round."""
        results = [
            {"match_key": "settlement:s1", "category": "DUPLICATE", "status": "EXCEPTION"},
            {"match_key": "settlement:s1_dup", "category": None, "status": "MATCHED"},
        ]
        report = score(results, [gt("settlement:s1", group="dup_0"), gt("settlement:s1_dup", group="dup_0")])
        self.assertEqual(report["duplicate_pairs"]["correct"], 1)


class TestPerLabelPrecisionRecall(unittest.TestCase):
    def test_false_positive_lowers_precision_not_recall(self):
        results = [
            {"match_key": "settlement:s1", "category": "UNEXPLAINED", "status": "EXCEPTION"},
            {"match_key": "settlement:s2", "category": "UNEXPLAINED", "status": "EXCEPTION"},
        ]
        ground_truth = [gt("settlement:s1", "UNEXPLAINED"), gt("settlement:s2", "UTR_LEVEL_MISMATCH")]
        report = score(results, ground_truth)
        self.assertEqual(report["per_label"]["UNEXPLAINED"]["precision"], 0.5)
        self.assertEqual(report["per_label"]["UNEXPLAINED"]["recall"], 1.0)

    def test_pipe_joined_rows_excluded_from_per_label_table(self):
        """A row scored against a set of allowed outcomes has no single
        true label to attribute precision/recall to -- it must not silently
        create a fake category made of the raw pipe string."""
        results = [{"match_key": "settlement:s1", "category": None, "status": "MATCHED_AI_ASSISTED"}]
        report = score(results, [gt("settlement:s1", "MATCHED_AI_ASSISTED|FUZZY_MATCH_NEEDS_REVIEW")])
        self.assertNotIn("MATCHED_AI_ASSISTED|FUZZY_MATCH_NEEDS_REVIEW", report["per_label"])


class TestRenderReport(unittest.TestCase):
    def test_renders_without_crashing_and_names_headline_number(self):
        results = [{"match_key": "settlement:s1", "category": None, "status": "MATCHED"}]
        report = score(results, [gt("settlement:s1", "MATCHED")])
        markdown = render_report(report)
        self.assertIn("Overall accuracy: 100.0%", markdown)

    def test_missing_keys_surfaced_in_report_text(self):
        report = score([], [gt("settlement:ghost", "MATCHED")])
        markdown = render_report(report)
        self.assertIn("settlement:ghost", markdown)


if __name__ == "__main__":
    unittest.main()
