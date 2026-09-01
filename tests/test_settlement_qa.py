"""
Unit tests for settlement_qa.py. Seeds a temporary database with known
rows and checks every answer against that known state -- never against
the real data/reconcile.db.
"""
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import db  # noqa: E402
import qa_intent_gate  # noqa: E402
import settlement_qa as qa  # noqa: E402


def make_result(order_id, status, category=None, narration="", net=100.0):
    return {
        "order_id": order_id, "settlement_id": f"setl_{order_id}", "net": net,
        "match_key": f"settlement:setl_{order_id}",
        "status": status, "category": category, "reason": f"test reason for {order_id}",
        "narration": narration, "stage": ["test stage"],
    }


class SettlementQaTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = Path(self._tmpdir.name) / "test_reconcile.db"
        db.persist_results([
            make_result("order_1", "MATCHED"),
            make_result("order_2", "EXCEPTION", category="DUPLICATE"),
            make_result("order_3", "EXCEPTION", category="DUPLICATE"),
            make_result("order_4", "EXCEPTION", category="ON_HOLD_BY_RAZORPAY"),
            make_result("order_5", "MATCHED_LOW_CONFIDENCE", category="FUZZY_MATCH_NEEDS_REVIEW"),
        ], run_id="run-1")

    def tearDown(self):
        db.DB_PATH = self._original_db_path
        self._tmpdir.cleanup()


class TestOrderLookup(SettlementQaTestCase):
    def test_known_order_returns_real_status_and_reason(self):
        result = qa.answer("what happened to order_2")
        self.assertIn("DUPLICATE", result)
        self.assertIn("test reason for order_2", result)

    def test_unknown_order_is_honest_not_fabricated(self):
        result = qa.answer("what happened to order_999")
        self.assertIn("No record of order_999", result)

    def test_confirmed_order_shows_human_decision(self):
        row_id = [r for r in db.get_all_exceptions() if r["order_id"] == "order_2"][0]["id"]
        db.resolve_exception(row_id, "confirm", note="looks right")
        result = qa.answer("why is order_2 unresolved")
        self.assertIn("CONFIRMED", result)
        self.assertIn("looks right", result)

    def test_money_question_naming_an_order_shows_the_amount(self):
        """Regression test for a real bug found by audit: a money question
        naming a specific order ("how much money is stuck in order_4") is
        intercepted by _answer()'s top-level order_id branch before
        _cash_value (which only handles category-scoped or overall totals,
        not a single order) ever runs -- so the actual number asked about
        used to never appear anywhere in the answer."""
        result = qa.answer("how much money is stuck in order_4")
        self.assertIn("Rs.100.00", result)

    def test_order_id_recognized_with_voice_style_punctuation_and_filler(self):
        """Regression test for a real bug found live over voice input:
        ORDER_ID_PATTERN used to require the digits immediately after
        "order" with only a single optional space or underscore --
        anything a browser's speech-to-text commonly inserts around a
        spoken order number ("order #1032", "order number 1032", "order,
        1032") silently failed to extract at all, sending an answerable
        question straight to the honest "don't know" fallback instead."""
        for phrasing in (
            "what happened to order #2",
            "what happened to order number 2",
            "what happened to order no 2",
            "what happened to order, 2",
        ):
            with self.subTest(phrasing=phrasing):
                result = qa.answer(phrasing)
                self.assertIn("DUPLICATE", result)
                self.assertNotEqual(result, qa.FALLBACK_MESSAGE)


class TestCategoryCount(SettlementQaTestCase):
    def test_duplicate_count_matches_real_data(self):
        result = qa.answer("how many DUPLICATE exceptions")
        self.assertIn("2 rows", result)

    def test_on_hold_phrasing_variant(self):
        result = qa.answer("what's on hold right now")
        self.assertIn("1 settlement on hold", result)

    def test_category_name_alone_is_not_treated_as_a_count_request(self):
        """Regression test for a live bug: merely mentioning a category
        name used to be enough to trigger a bare count answer, so a
        statement or an unrelated question naming a category got a
        nonsense reply instead of an honest "don't know". A category name
        is not itself a count/list request."""
        result = qa.answer("I really don't like this DUPLICATE thing")
        self.assertEqual(result, qa.FALLBACK_MESSAGE)

    def test_on_hold_as_a_statement_is_not_treated_as_a_count_request(self):
        """Regression test for a real bug found by audit: the
        ON_HOLD_BY_RAZORPAY branch had no count-intent check at all --
        _extract_category's own fallback sets this category from the
        literal phrase "on hold", so ANY sentence containing it got a
        bare, often nonsensical count. Neither of these is a status
        question; the second isn't even about settlements."""
        self.assertEqual(qa.answer("my settlement is on hold, is that bad"), qa.FALLBACK_MESSAGE)
        self.assertEqual(
            qa.answer("I've been on hold with support for an hour, can someone help"),
            qa.FALLBACK_MESSAGE,
        )

    def test_category_name_does_not_match_inside_an_unrelated_word(self):
        """Regression test for a real bug found by audit: "ROUNDING" is a
        bare substring of the ordinary English word "surrounding", with no
        word-boundary check -- so a question using that word got silently
        hijacked into a ROUNDING-only answer. The real open count must
        survive (checked against db.get_open_exceptions() directly, not a
        hand-picked number), not get replaced by a bogus, always-zero
        ROUNDING-scoped one -- there is no ROUNDING row in this fixture at
        all, so the bug's old answer would have been "0 rows"."""
        expected = len(db.get_open_exceptions())
        result = qa.answer("how many exceptions are surrounding this batch, in general")
        self.assertIn(qa._plural(expected, "row"), result)
        self.assertNotIn("ROUNDING", result)


class TestOpenCount(SettlementQaTestCase):
    def test_open_count_matches_needs_action_rows(self):
        result = qa.answer("how many are open")
        expected = len(db.get_open_exceptions())
        self.assertIn(qa._plural(expected, "row"), result)

    def test_natural_phrasing_variants_are_recognized(self):
        """Regression test for a real bug found live over voice: natural
        phrasings like "how many orders need my attention" used to miss
        the exact five-phrase list _open_count required, silently falling
        through to the honest fallback instead of the real count."""
        expected = len(db.get_open_exceptions())
        for phrasing in (
            "can you tell me how many orders need my attention",
            "how many are still outstanding",
        ):
            with self.subTest(phrasing=phrasing):
                result = qa.answer(phrasing)
                self.assertIn(qa._plural(expected, "row"), result)


class TestResolutionRate(SettlementQaTestCase):
    def test_resolution_rate_matches_manual_calculation(self):
        rows = db.get_all_exceptions()
        resolved = sum(1 for r in rows if r["status"] in qa.RESOLVED_STATUSES)
        expected_pct = round(100 * resolved / len(rows), 1)
        result = qa.answer("what's my resolution rate")
        self.assertIn(f"{expected_pct}%", result)

    def test_resolution_rate_excludes_unconfirmed_fuzzy_matches(self):
        """Regression test for the same metrics bug already fixed in
        reconcile.py and review_server.py, caught here too:
        MATCHED_LOW_CONFIDENCE must not count as resolved. Of the 5 seeded
        rows, only order_1 (plain MATCHED) is genuinely resolved with zero
        human input -- 1 of 5, 20.0%, computed independently of
        RESOLVED_STATUSES so a reintroduced bug can't pass by agreeing
        with itself."""
        result = qa.answer("what's my resolution rate")
        self.assertIn("20.0%", result)
        self.assertNotIn("MATCHED_LOW_CONFIDENCE", qa.RESOLVED_STATUSES)


class TestUnknownQuestion(SettlementQaTestCase):
    def test_unrecognized_question_admits_it_not_guesses(self):
        result = qa.answer("what is the capital of France")
        self.assertIn("don't have a way to answer", result)


class TestSmallTalk(SettlementQaTestCase):
    """A bare greeting has no order_id, settlement_id, or category to
    find, so it used to fall straight through to the honest "I don't
    have a way to answer that" -- technically correct but a bad first
    impression, especially as the very first thing said to the voice
    agent. Fixed, human-written responses, never model-generated."""

    def test_hello_gets_a_warm_reply_not_the_fallback(self):
        result = qa.answer("hello")
        self.assertNotEqual(result, qa.FALLBACK_MESSAGE)
        self.assertIn("Hello", result)

    def test_thanks_is_acknowledged(self):
        result = qa.answer("thank you")
        self.assertIn("welcome", result.lower())

    def test_goodbye_is_acknowledged(self):
        result = qa.answer("goodbye")
        self.assertIn("Goodbye", result)

    def test_what_can_you_do_gets_a_real_capability_answer(self):
        result = qa.answer("what can you do")
        self.assertNotEqual(result, qa.FALLBACK_MESSAGE)
        self.assertIn("order", result.lower())

    def test_short_trigger_words_do_not_collide_with_real_questions(self):
        """Regression guard: "hi" and "hey" are short enough to risk
        matching inside an unrelated word or phrase if this weren't
        word-boundary matched -- the same class of bug _extract_category
        already guards against for "ROUNDING" inside "surrounding"."""
        for phrasing in (
            "help me find order_2", "can you help me understand this category",
            "what happened to order_2", "how can it be resolved",
        ):
            with self.subTest(phrasing=phrasing):
                self.assertIsNone(qa._small_talk(phrasing))

    def test_bare_acknowledgment_gets_a_short_warm_reply(self):
        for phrasing in ("ok", "okay", "sure", "alright", "got it", "OK.", "sounds good"):
            with self.subTest(phrasing=phrasing):
                result = qa.answer(phrasing)
                self.assertNotEqual(result, qa.FALLBACK_MESSAGE)

    def test_acknowledgment_word_inside_a_real_question_does_not_collide(self):
        """_ACK_RE is anchored to the whole utterance specifically because
        "ok"/"sure" are ordinary words that show up inside real questions --
        this must still reach the real order lookup, not the ack reply."""
        for phrasing in (
            "is it okay to reject order_2", "sure, what happened to order_2",
        ):
            with self.subTest(phrasing=phrasing):
                self.assertIsNone(qa._small_talk(phrasing))


class TestGlossary(SettlementQaTestCase):
    """Plain fintech/reconciliation vocabulary -- "what is a UTR" -- is a
    real, common question that has nothing to do with this batch's own
    numbers, so answering it can't risk inventing a figure. Distinct from
    CATEGORY_GUIDANCE (a specific category's meaning, once named) and
    PROJECT_KNOWLEDGE (this system's own architecture)."""

    def test_utr_definition(self):
        result = qa.answer("what is a UTR")
        self.assertNotEqual(result, qa.FALLBACK_MESSAGE)
        self.assertIn("Unique Transaction Reference", result)

    def test_chargeback_definition(self):
        result = qa.answer("what does chargeback mean")
        self.assertIn("reversal", result.lower())

    def test_afa_mandate_definition(self):
        result = qa.answer("what is AFA")
        self.assertIn("Additional Factor of Authentication", result)

    def test_audit_trail_definition(self):
        result = qa.answer("what is an audit trail")
        self.assertIn("Replay log", result)

    def test_real_batch_question_is_not_shadowed_by_the_glossary(self):
        """"what is the total settlement value" must still reach the real
        _batch_totals number, not a generic definition of "settlement" --
        _glossary is dispatched last, after every batch-data handler."""
        result = qa.answer("what is the total settlement value")
        self.assertNotIn("Reconciliation is confirming", result)
        self.assertNotIn("The internal ledger is", result)


class TestProjectKnowledge(SettlementQaTestCase):
    """Fixed, human-written answers about the system itself (architecture,
    model choice, metrics, research) -- never model-generated, same
    principle as CATEGORY_GUIDANCE."""

    def test_why_not_llm(self):
        result = qa.answer("why isn't this just an LLM")
        self.assertIn("deterministic", result)
        self.assertNotIn("don't have a way to answer", result)

    def test_what_model_question(self):
        result = qa.answer("what model do you use")
        self.assertIn("qwen2.5:0.5b", result)

    def test_architecture_question(self):
        result = qa.answer("what's the architecture")
        self.assertIn("UTR", result)

    def test_accuracy_question(self):
        result = qa.answer("what's your accuracy")
        self.assertIn("90.5", result)

    def test_slash_research_question(self):
        result = qa.answer("tell me about slash")
        self.assertIn("Slash", result)

    def test_apostrophe_dropped_phrasing_still_matches(self):
        """Regression test for a real gap found live: every trigger phrase
        here is written as "what's"/"isn't", so voice transcription and
        plain casual typing that drops the apostrophe ("whats the
        architecture", "why isnt this just an llm") used to silently miss
        every one of them. _normalize() strips apostrophes from the
        question before matching, so both forms must now work."""
        for phrasing in ("whats the architecture", "whats your accuracy", "why isnt this just an llm"):
            with self.subTest(phrasing=phrasing):
                result = qa.answer(phrasing)
                self.assertNotEqual(result, qa.FALLBACK_MESSAGE)

    def test_why_question_does_not_get_misrouted_to_resolution_guidance(self):
        """Regression test for a real dispatch collision: a bare "why" in
        a project question used to trip _is_resolution_question's generic
        why_signal (no order_id present) and get answered "tell me which
        order or category you mean" instead of a real answer -- project
        knowledge must be checked first."""
        result = qa.answer("why isn't this just an LLM")
        self.assertNotIn("tell me which order or category", result.lower())


class TestResolutionGuidance(SettlementQaTestCase):
    def test_guidance_for_explicit_order(self):
        result = qa.answer("how can order_2 be resolved")
        self.assertIn("DUPLICATE", result)
        self.assertIn("excluded from cash totals", result)

    def test_guidance_for_explicit_category(self):
        result = qa.answer("how can a DUPLICATE be resolved")
        self.assertIn("excluded from cash totals", result)

    def test_guidance_without_any_referent_asks_for_one(self):
        result = qa.answer("how can it be resolved")
        self.assertIn("Tell me which order or category", result)

    def test_unknown_order_in_guidance_question_is_honest(self):
        result = qa.answer("how can order_999 be resolved")
        self.assertIn("No record of order_999", result)

    def test_what_can_i_do_meanwhile_matches_guidance(self):
        result = qa.answer("what can i do by that time for order_4")
        self.assertIn("cash flow forecast", result)
        self.assertIn("ON_HOLD_BY_RAZORPAY", result)

    def test_will_it_affect_my_cash_matches_guidance(self):
        result = qa.answer("will it affect my cash flow for order_4")
        self.assertIn("not yet in your bank account", result)

    def test_affect_question_uses_context_without_repeating_order(self):
        _, ctx = qa.answer_with_context("what happened to order_4")
        result, _ = qa.answer_with_context("will this affect my system?", ctx)
        self.assertIn("ON_HOLD_BY_RAZORPAY", result)
        self.assertIn("cash flow forecast", result)

    def test_picks_the_categorized_row_when_an_order_has_two(self):
        """Regression test for a real bug found by audit: a DUPLICATE
        settlement and its clean-matched sibling share one order_id (see
        reconcile.py's DUPLICATE detection). This blindly took rows[0] --
        whichever row was inserted first, data-dependent -- so a known
        DUPLICATE order got "tell me which order or category you mean"
        whenever its plain MATCHED, category=None sibling happened to sort
        first. _similar_orders already has this exact fix (see its own
        test); this handler never got it."""
        db.persist_results([
            {"order_id": "order_20", "settlement_id": "setl_20a", "net": 100.0,
             "match_key": "settlement:setl_20a", "status": "MATCHED", "category": None,
             "reason": None, "narration": "", "stage": []},
            {"order_id": "order_20", "settlement_id": "setl_20a_dup", "net": 100.0,
             "match_key": "settlement:setl_20a_dup", "status": "EXCEPTION", "category": "DUPLICATE",
             "reason": "duplicate export row", "narration": "", "stage": []},
        ], run_id="run-1")
        result = qa.answer("how can order_20 be resolved")
        self.assertIn("DUPLICATE", result)
        self.assertIn("excluded from cash totals", result)

    def test_why_question_for_explicit_category_gets_real_guidance(self):
        """Regression test for a live bug: "why u think tehy are duplicate"
        used to get answered "11 rows categorized as DUPLICATE" by
        _category_count, since it happens to mention the category name --
        not an answer to "why" at all. "why" is now a resolution-question
        trigger, so this routes to the real CATEGORY_GUIDANCE text, which
        already opens with what the category means."""
        result = qa.answer("why u think tehy are duplicate")
        self.assertIn("excluded from cash totals", result)
        self.assertNotIn("categorized as", result)

    def test_bare_why_follow_up_uses_context_category(self):
        _, ctx = qa.answer_with_context("list DUPLICATE orders")
        result, _ = qa.answer_with_context("but why", ctx)
        self.assertIn("excluded from cash totals", result)


class TestSimilarOrders(SettlementQaTestCase):
    def test_picks_the_categorized_row_when_an_order_has_two(self):
        """A DUPLICATE settlement and its clean-matched sibling share one
        order_id -- see reconcile.py's DUPLICATE detection. Found live
        against the real database: this used to silently pick whichever
        row was inserted first (the plain MATCHED sibling, category=None),
        reporting "no category" for an order that actually has one.
        match_key must differ per row for both to persist under one
        order_id, exactly like the real DUPLICATE/sibling pair does."""
        db.persist_results([
            {"order_id": "order_20", "settlement_id": "setl_20a", "net": 100.0,
             "match_key": "settlement:setl_20a", "status": "MATCHED", "category": None,
             "reason": None, "narration": "", "stage": []},
            {"order_id": "order_20", "settlement_id": "setl_20a_dup", "net": 100.0,
             "match_key": "settlement:setl_20a_dup", "status": "EXCEPTION", "category": "DUPLICATE",
             "reason": "duplicate export row", "narration": "", "stage": []},
        ], run_id="run-1")
        result = qa.answer("any similar orders to order_20")
        self.assertIn("order_20 is categorized DUPLICATE", result)

    def test_same_category_match_is_found(self):
        """order_2 and order_3 are both DUPLICATE in the shared fixture."""
        result = qa.answer("any similar orders to order_2")
        self.assertIn("order_3", result)
        self.assertIn("1 other row shares that exact category", result)

    def test_no_match_is_reported_honestly_not_guessed(self):
        """order_1 has no category and no narration -- nothing to match on,
        and the fixture's other rows don't share MATCHED as a category
        (category is None for order_1, which is falsy and deliberately
        excluded from the same-category comparison)."""
        result = qa.answer("is order_1 similar to any other order")
        self.assertIn("doesn't share a category or a closely worded narration", result)

    def test_unknown_order_is_honest_not_fabricated(self):
        result = qa.answer("any similar orders to order_999")
        self.assertIn("No record of order_999", result)

    def test_no_referent_asks_for_one(self):
        result = qa.answer("has this happened before")
        self.assertIn("Tell me which order you mean", result)

    def test_follow_up_uses_prior_order_from_context(self):
        _, ctx = qa.answer_with_context("what happened to order_2")
        result, _ = qa.answer_with_context("has this happened before", ctx)
        self.assertIn("order_3", result)

    def test_narration_similarity_uses_the_same_difflib_function_reconcile_uses(self):
        """Two new rows, different categories on purpose, so a category
        match can't be the reason they show up together -- only a close
        narration match can explain it."""
        db.persist_results([
            make_result("order_10", "EXCEPTION", category="UNEXPLAINED",
                        narration="pymt rcvd customer ord#1042 thx"),
            make_result("order_11", "EXCEPTION", category="TAX_DEDUCTION",
                        narration="pymt rcvd customer ord#1043 thx"),
        ], run_id="run-1")
        result = qa.answer("any similar orders to order_10")
        self.assertIn("order_11", result)
        self.assertIn("Narration wording is closely similar to", result)


class TestSettlementLookup(SettlementQaTestCase):
    def test_known_settlement_returns_real_status_and_reason(self):
        db.persist_results([
            {"order_id": "order_30", "settlement_id": "setl_a1b2c3d4e5f6a7", "net": 100.0,
             "match_key": "settlement:setl_a1b2c3d4e5f6a7", "status": "EXCEPTION",
             "category": "UNEXPLAINED", "reason": "no reference found",
             "narration": "", "stage": []},
        ], run_id="run-1")
        result = qa.answer("what happened to setl_a1b2c3d4e5f6a7")
        self.assertIn("order_30", result)
        self.assertIn("UNEXPLAINED", result)
        self.assertIn("no reference found", result)

    def test_unknown_settlement_is_honest_not_fabricated(self):
        result = qa.answer("what happened to setl_doesnotexist99")
        self.assertIn("No record of setl_doesnotexist99", result)

    def test_settlement_lookup_does_not_shadow_order_lookup(self):
        """A question naming both an order_id and something that merely
        looks like a settlement_id should still resolve as an order
        lookup -- order_id is the primary key everything else here is
        built around."""
        result = qa.answer("what happened to order_2")
        self.assertIn("DUPLICATE", result)


class TestCategoryList(SettlementQaTestCase):
    def test_list_phrasing_returns_order_ids_not_just_a_count(self):
        result = qa.answer("list DUPLICATE orders")
        self.assertIn("order_2", result)
        self.assertIn("order_3", result)
        self.assertIn("2 rows", result)

    def test_which_orders_phrasing_also_triggers_a_list(self):
        result = qa.answer("which orders are ON_HOLD_BY_RAZORPAY")
        self.assertIn("order_4", result)

    def test_plain_count_phrasing_is_unaffected_by_the_list_feature(self):
        """Regression guard: adding list support to the same handler must
        not change the existing count-only answer for the original
        phrasing this function already had a test for."""
        result = qa.answer("how many DUPLICATE exceptions")
        self.assertIn("2 rows", result)
        self.assertNotIn("order_2", result)

    def test_list_of_empty_category_is_honest_not_fabricated(self):
        result = qa.answer("list ROUNDING orders")
        self.assertIn("No rows are categorized as ROUNDING", result)

    def test_plural_category_name_is_recognized(self):
        """Regression test for a real bug found live over voice:
        _extract_category only matched the exact singular category token
        (DUPLICATE), so natural plurals like "duplicates" -- normal
        spoken/typed phrasing -- silently failed to extract a category at
        all and fell through to the honest fallback."""
        result = qa.answer("what about duplicates")
        self.assertIn("order_2", result)
        self.assertIn("order_3", result)

    def test_what_about_phrasing_triggers_a_list(self):
        """"what about X" is a vague but common natural way to ask about a
        category; the most useful default is treating it like a list
        request, same as "which orders are X"."""
        result = qa.answer("what about ON_HOLD_BY_RAZORPAY")
        self.assertIn("order_4", result)


class TestResolutionStatusCount(SettlementQaTestCase):
    def test_confirmed_count(self):
        row_id = [r for r in db.get_all_exceptions() if r["order_id"] == "order_2"][0]["id"]
        db.resolve_exception(row_id, "confirm")
        result = qa.answer("how many have been confirmed")
        self.assertIn("1 row has been confirmed", result)

    def test_rejected_count(self):
        row_id = [r for r in db.get_all_exceptions() if r["order_id"] == "order_3"][0]["id"]
        db.resolve_exception(row_id, "reject")
        result = qa.answer("how many rejected")
        self.assertIn("1 row has been rejected", result)

    def test_natural_phrasing_variants_are_recognized(self):
        """Regression test for a real bug found live over voice: natural
        phrasings like "how many have i confirmed so far" used to miss the
        exact three-phrase-per-branch list this handler required, silently
        falling through to the honest fallback instead of the real count."""
        row_id = [r for r in db.get_all_exceptions() if r["order_id"] == "order_2"][0]["id"]
        db.resolve_exception(row_id, "confirm")
        result = qa.answer("how many have i confirmed so far")
        self.assertIn("1 row has been confirmed", result)

    def test_needs_clarification_count_is_open_rows_with_a_note(self):
        """"Needs clarification" isn't a resolution_status value in the
        schema -- add_note() deliberately leaves the row OPEN (see its
        own docstring). This counts OPEN-with-a-note, not a status that
        doesn't exist."""
        row_id = [r for r in db.get_all_exceptions() if r["order_id"] == "order_4"][0]["id"]
        db.add_note(row_id, "waiting on merchant reply")
        result = qa.answer("how many rows need clarification")
        self.assertIn("1 row is still open with a clarification note", result)

    def test_zero_confirmed_is_reported_honestly(self):
        result = qa.answer("how many have been confirmed")
        self.assertIn("0 rows have been confirmed", result)


class TestCashValue(SettlementQaTestCase):
    def test_category_scoped_sum(self):
        """order_2 and order_3 are both DUPLICATE at net=100.0 each in the
        shared fixture."""
        result = qa.answer("how much money is in DUPLICATE")
        self.assertIn("Rs.200.00", result)
        self.assertIn("DUPLICATE", result)

    def test_category_with_no_rows_is_honest_zero(self):
        result = qa.answer("cash value of ROUNDING")
        self.assertIn("Rs.0.00", result)

    def test_overall_cash_position_matches_compute_cash_clarity_directly(self):
        """Cross-checked against db.compute_cash_clarity() called directly
        on the same rows, not against a hand-picked expected number --
        this is the exact function the Overview page's cash panel uses,
        so if this test and that panel ever disagree, one of them has a
        real bug."""
        rows = db.get_all_exceptions()
        expected = db.compute_cash_clarity(rows)
        result = qa.answer("how much cash is at risk")
        self.assertIn(f"Rs.{expected['at_risk']:,.2f}", result)
        self.assertIn(f"Rs.{expected['resolved']:,.2f}", result)
        self.assertIn(f"Rs.{expected['still_open']:,.2f}", result)

    def test_duplicate_rows_excluded_from_overall_cash_position(self):
        """Regression guard for the same fix compute_cash_clarity() itself
        already has a test for: DUPLICATE rows must not double-count
        money that already cleared under their sibling row. Asserted here
        too since this is a second, independent call site of that
        function -- a future change that broke the DUPLICATE exclusion
        only at this call site (e.g. a copy-paste that dropped the
        filter) would still be caught."""
        result = qa.answer("what's my cash position")
        rows = db.get_all_exceptions()
        duplicate_total = sum(r["net_amount"] for r in rows if r["category"] == "DUPLICATE")
        expected = db.compute_cash_clarity(rows)
        self.assertNotIn(f"Rs.{expected['at_risk'] + duplicate_total:,.2f}", result)


class TestBatchSummary(SettlementQaTestCase):
    def test_overview_includes_resolved_percent_and_open_count(self):
        rows = db.get_all_exceptions()
        resolved = sum(1 for r in rows if r["status"] in qa.RESOLVED_STATUSES)
        pct = round(100 * resolved / len(rows), 1)
        open_rows = db.get_open_exceptions()
        result = qa.answer("give me an overview of this batch")
        self.assertIn(f"{qa._plural(len(rows), 'row')} in this batch", result)
        self.assertIn(f"{pct}% resolved", result)
        self.assertIn(f"{qa._plural(len(open_rows), 'row')} still need a decision", result)

    def test_summary_matches_compute_cash_clarity_directly(self):
        """Composed from the same function the cash-position handler and
        the Overview page both use -- not a fifth reimplementation."""
        rows = db.get_all_exceptions()
        expected = db.compute_cash_clarity(rows)
        result = qa.answer("how does this batch look")
        self.assertIn(f"Rs.{expected['at_risk']:,.2f}", result)
        self.assertIn(f"Rs.{expected['resolved']:,.2f}", result)


class TestAiNarrativeSummary(SettlementQaTestCase):
    """_ai_narrative_summary is a genuinely heavier AI role than Pass 4's
    candidate-picking -- the model writes prose, not just a label. Gated
    more strictly than Pass 4, though: every number in its output is
    fact-checked against the real, already-computed facts before it's
    ever shown. These tests mock the Ollama HTTP call -- a live model
    call has no place in a unit test -- except the one at the bottom,
    which is a genuine end-to-end run, skipped if Ollama isn't reachable."""

    def test_trigger_reaches_the_handler(self):
        with patch.object(qa, "_generate_narrative", return_value=None):
            result = qa.answer("narrate this batch for me")
        # falls back to the deterministic template, but must not be the
        # generic fallback -- proves the trigger actually routed here
        self.assertNotEqual(result, qa.FALLBACK_MESSAGE)
        self.assertIn("rows in this batch", result)

    def test_honest_narrative_using_only_real_numbers_is_accepted(self):
        facts = qa._batch_facts()
        honest_text = (
            f"This batch has {facts['total_rows']} rows, {facts['resolved_pct']}% resolved."
        )
        with patch.object(qa, "_generate_narrative", return_value=honest_text):
            result = qa.answer("narrate this batch for me")
        self.assertEqual(result, honest_text)

    def test_real_hallucination_found_live_is_rejected_not_shown(self):
        """This exact text is what the real local model (qwen2.5:0.5b)
        actually produced the first time this feature was tested live,
        given the real facts for this fixture's batch shape -- not a
        hypothetical adversarial case. It invented "54.5%" (not derivable
        from any real number it was given) and confused a rupee amount
        for a transaction count. The validator must reject the whole
        response, not show a partially-wrong answer with the one bad
        number quietly left in."""
        facts = qa._batch_facts()
        real_model_output = (
            "A settlement reconciliation batch for a merchant revealed that 90.5% of the "
            "transactions were resolved, with 465 resolved transactions out of a total of 514. "
            "The remaining 54.5% of the transactions were open and at risk, with a total of "
            "291,313.9 transactions. The merchant's cash balance was 83,681.66, and the "
            "remaining balance was 30,254.68. The remaining 177,377.56 transactions were still "
            "open, with a total of 177,377.56."
        )
        with patch.object(qa, "_generate_narrative", return_value=real_model_output):
            result = qa.answer("narrate this batch for me")
        self.assertNotEqual(result, real_model_output)
        self.assertNotIn("54.5", result)
        self.assertIn("rows in this batch", result)  # the honest deterministic fallback

    def test_ollama_unreachable_falls_back_cleanly(self):
        with patch.object(qa, "_generate_narrative", return_value=None):
            result = qa.answer("tell me a story about this batch")
        self.assertIn("rows in this batch", result)

    def test_trailing_sentence_period_is_not_mistaken_for_part_of_the_number(self):
        """Regression test for a real bug found live in the same test run
        that surfaced the hallucination above: "...a total of 514. The
        remaining..." used to have its number-matching regex swallow the
        sentence-ending period, capturing "514." instead of "514" -- never
        in the allowed set no matter how the real 514 is formatted, a
        false rejection of an honestly-reported number."""
        self.assertEqual(qa._numbers_in("a total of 514. The remaining"), ["514"])


def _ollama_is_running():
    try:
        urllib.request.urlopen("http://127.0.0.1:11434", timeout=2)
        return True
    except Exception:
        return False


@unittest.skipUnless(_ollama_is_running(), "Ollama is not running on 127.0.0.1:11434")
class TestAiNarrativeSummaryRealOllama(SettlementQaTestCase):
    """A genuine end-to-end run against the real local model, skipped
    (not faked) if it isn't reachable -- mirrors test_qa_intent_gate.py's
    own TestRealOllamaIntegration pattern."""

    def test_real_call_either_passes_validation_or_falls_back_cleanly(self):
        """Either outcome is legitimate here -- the point of this test is
        only that a live call through the real model, then through the
        real validator, completes without error, returns a non-empty
        answer, and never contains an invented number. It deliberately
        doesn't assert specific wording: a validated AI response's own
        phrasing isn't under this code's control, only its numbers are
        -- that's the actual scope of what the validator checks, found
        live while writing this same test (see the note on
        _allowed_numeric_tokens: a real, correctly-matched number can
        still be attached to the wrong concept by the model's own
        phrasing, and that's a different, harder problem than the one
        this gate solves)."""
        facts = qa._batch_facts()
        result = qa.answer("narrate this batch for me")
        self.assertTrue(result)
        allowed = qa._allowed_numeric_tokens(facts)
        self.assertTrue(all(tok in allowed for tok in qa._numbers_in(result)))


class TestStatusBreakdown(SettlementQaTestCase):
    def test_status_breakdown_matches_real_counts(self):
        result = qa.answer("what's the status breakdown")
        self.assertIn("EXCEPTION: 3", result)
        self.assertIn("MATCHED: 1", result)
        self.assertIn("MATCHED_LOW_CONFIDENCE: 1", result)

    def test_how_many_matched_phrasing_also_triggers_status_breakdown(self):
        result = qa.answer("how many are matched")
        self.assertIn("MATCHED: 1", result)

    def test_status_breakdown_does_not_collide_with_category_breakdown(self):
        """Regression guard: a bare "breakdown" substring used to be
        _category_breakdown's own trigger, which would have swallowed a
        "status breakdown" question and answered with categories instead
        of statuses -- a real dispatch collision, caught before shipping."""
        result = qa.answer("what's the status breakdown")
        self.assertNotIn("DUPLICATE:", result)


class TestBatchTotals(SettlementQaTestCase):
    def test_settlement_count_matches_real_data(self):
        result = qa.answer("how many settlements are in this batch")
        self.assertIn("5 rows", result)
        self.assertIn("5 orders", result)
        self.assertIn("5 settlements", result)

    def test_total_value_is_the_whole_batch_not_just_exceptions(self):
        """Distinct from _cash_value's "cash position"/"at risk" scope,
        which deliberately excludes clean MATCHED rows (see
        compute_cash_clarity's own docstring) -- this handler sums every
        row's net_amount, MATCHED rows included, so it must not equal the
        narrower at_risk figure."""
        rows = db.get_all_exceptions()
        clarity = db.compute_cash_clarity(rows)
        whole_batch_total = sum(r["net_amount"] for r in rows if r["net_amount"] is not None)
        self.assertNotEqual(whole_batch_total, clarity["at_risk"])
        result = qa.answer("what's the total settlement value")
        self.assertIn(f"Rs.{whole_batch_total:,.2f}", result)


class TestExtremeAmount(SettlementQaTestCase):
    def test_largest_and_smallest_amount_in_a_batch_with_distinct_values(self):
        db.persist_results([
            make_result("order_10", "EXCEPTION", category="DUPLICATE", net=9999.0),
            make_result("order_11", "EXCEPTION", category="UNEXPLAINED", net=1.0),
        ], run_id="run-2")
        biggest = qa.answer("what's the biggest exception")
        self.assertIn("order_10", biggest)
        self.assertIn("9,999.00", biggest)
        smallest = qa.answer("what's the smallest amount")
        self.assertIn("order_11", smallest)
        self.assertIn("1.00", smallest)

    def test_extreme_amount_scoped_to_a_category(self):
        db.persist_results([
            make_result("order_10", "EXCEPTION", category="DUPLICATE", net=9999.0),
        ], run_id="run-2")
        result = qa.answer("what's the largest DUPLICATE amount")
        self.assertIn("order_10", result)


class TestRecurringPatterns(SettlementQaTestCase):
    """Found live on the real batch: every open FUZZY_MATCH_NEEDS_REVIEW
    row shared the identical narration shape "pymt rcvd # ord## thx" --
    not 14 independent typos, one systemic upstream issue. These tests
    reproduce that same shape of finding against the small seeded
    fixture, not just assert on the real batch's own numbers, which
    would break the moment the batch is regenerated."""

    def test_shared_narration_shape_across_open_rows_is_flagged(self):
        db.persist_results([
            make_result("order_20", "EXCEPTION", category="FUZZY_MATCH_NEEDS_REVIEW",
                         narration="pymt rcvd 3 ord#l020 thx"),
            make_result("order_21", "EXCEPTION", category="FUZZY_MATCH_NEEDS_REVIEW",
                         narration="pymt rcvd 9 ord#l021 thx"),
        ], run_id="run-2")
        result = qa.answer("is there any recurring pattern in the exceptions")
        self.assertIn("recurring pattern", result)
        self.assertIn("order_20", result)
        self.assertIn("order_21", result)

    def test_unrelated_narrations_are_not_flagged_as_a_pattern(self):
        """Regression guard: two open rows that just happen to both be
        open must not be reported as "recurring" -- only rows that
        genuinely share the same generalized shape should group."""
        db.persist_results([
            make_result("order_30", "EXCEPTION", category="UNEXPLAINED",
                         narration="no reference found anywhere in this line"),
        ], run_id="run-2")
        result = qa.answer("any systemic issues here")
        self.assertIn("one-off", result)

    def test_narration_shape_generalizes_any_digit_bearing_token_not_just_a_known_order_ref(self):
        """Unlike db._derive_template() (Pass 2.6's learned-template
        generalizer), this doesn't need to already know which digits
        belong to which order, and doesn't require them to be cleanly
        present -- exactly the case an open, unresolved row usually is,
        since a cleanly-present reference is what let Pass 2.75 resolve
        it before it could ever reach the queue at all."""
        self.assertEqual(qa._narration_shape("pymt rcvd 3 ord#l171 thx"), "pymt rcvd # ord## thx")
        self.assertEqual(
            qa._narration_shape("pymt rcvd 3 ord#l171 thx"),
            qa._narration_shape("pymt rcvd 9 ord#l216 thx"),
        )


class TestTaxLineAudit(SettlementQaTestCase):
    """Track 4 names a "tax-line matcher" as its own use case. Mocks
    tax_audit.audit_tax_lines() with controlled fixture data -- the real
    finding (order_1210/order_1151, Rs.1 overcharged each) is verified
    directly against tax_audit.py's own tests, not re-asserted here
    against numbers that would break if the real batch is regenerated."""

    def test_reports_real_findings_with_numbers(self):
        fake_findings = [{
            "order_id": "order_9", "settlement_id": "setl_order_9", "mdr": 100.0,
            "expected_gst": 18.0, "actual_gst": 19.0, "diff": 1.0, "direction": "overcharged",
        }]
        with patch.object(qa.tax_audit, "audit_tax_lines", return_value=fake_findings):
            result = qa.answer("check tax rates")
        self.assertIn("order_9", result)
        self.assertIn("18.00", result)
        self.assertIn("19.00", result)
        self.assertIn("overcharged", result)

    def test_no_findings_says_so_honestly(self):
        with patch.object(qa.tax_audit, "audit_tax_lines", return_value=[]):
            result = qa.answer("is the gst correct")
        self.assertIn("No tax-line errors found", result)

    def test_trigger_phrases_all_route_to_the_audit(self):
        with patch.object(qa.tax_audit, "audit_tax_lines", return_value=[]) as mocked:
            for phrasing in ("tax line matcher", "any tax errors", "audit tax lines", "check gst rates"):
                with self.subTest(phrasing=phrasing):
                    qa.answer(phrasing)
            self.assertEqual(mocked.call_count, 4)


class TestLlmFallbackRouting(SettlementQaTestCase):
    """Tests settlement_qa.py's own fallback wiring, not qa_intent_gate.py's
    or qa_intent_router.py's internals (see test_qa_intent_router.py and
    test_qa_intent_gate.py for those) -- route_gated() is mocked here so
    these are deterministic and need no live Ollama."""

    def test_unrecognized_phrasing_routed_to_real_handler_via_mocked_gate(self):
        """"what's the deal with this batch" matches no keyword shape
        directly -- it names no category, order, count phrase, or list
        phrase the deterministic path recognizes -- so this only passes if
        the LLM fallback actually fires and its canonical reformulation
        reaches the real, unmocked _category_count handler, not a
        hand-rolled answer."""
        with patch.object(qa_intent_gate, "route_gated", return_value="list DUPLICATE orders"):
            result = qa.answer("what's the deal with this batch")
        self.assertIn("order_2", result)
        self.assertIn("order_3", result)

    def test_gate_holding_falls_through_to_honest_fallback(self):
        """route_gated() returning None -- whether because confidence was
        low, the tier isn't trusted (today: always, see qa_intent_gate.py),
        or Ollama wasn't reachable at all (e.g. on the Vercel deployment,
        which has no local model) -- must behave identically: never a
        crash, never a guess."""
        with patch.object(qa_intent_gate, "route_gated", return_value=None):
            result = qa.answer("what's the deal with this batch")
        self.assertEqual(result, qa.FALLBACK_MESSAGE)

    def test_llm_fallback_is_not_tried_when_a_keyword_shape_already_matched(self):
        """route_gated() must never even be called for a question the fast,
        deterministic keyword path already answers -- the LLM is a
        fallback for unrecognized phrasing only, not a call made on every
        turn regardless of need."""
        with patch.object(qa_intent_gate, "route_gated") as mock_gate:
            qa.answer("how many DUPLICATE exceptions")
        mock_gate.assert_not_called()

    def test_recursive_retry_is_capped_at_one_hop(self):
        """If the canonical reformulation somehow still doesn't match any
        keyword shape (a bug in the router, or a future settlement_qa.py
        change that drops a trigger phrase), the retry must not loop back
        into the LLM fallback a second time -- one attempt only, then the
        honest fallback."""
        with patch.object(qa_intent_gate, "route_gated", return_value="qwertyuiop not a real trigger phrase") as mock_gate:
            result = qa.answer("some nonsense the router also can't place")
        self.assertEqual(result, qa.FALLBACK_MESSAGE)
        mock_gate.assert_called_once()


class TestFollowUpContext(SettlementQaTestCase):
    def test_follow_up_resolves_using_prior_order(self):
        _, ctx = qa.answer_with_context("what happened to order_2")
        result, ctx2 = qa.answer_with_context("how can it be resolved", ctx)
        self.assertIn("order_2", result)
        self.assertIn("DUPLICATE", result)
        self.assertEqual(ctx2["last_order_id"], "order_2")

    def test_follow_up_resolves_using_prior_category(self):
        _, ctx = qa.answer_with_context("how many DUPLICATE exceptions")
        result, _ = qa.answer_with_context("how can it be resolved", ctx)
        self.assertIn("DUPLICATE", result)
        self.assertIn("excluded from cash totals", result)

    def test_stateless_answer_has_no_memory_across_calls(self):
        qa.answer("what happened to order_2")
        result = qa.answer("how can it be resolved")
        self.assertIn("Tell me which order or category", result)

    def test_unanswered_category_mention_does_not_clobber_prior_order_context(self):
        """Regression test for a real bug found live over voice: a
        question mentioning a category name in passing, with no
        count/list trigger word, used to overwrite last_category and pop
        last_order_id from the context BEFORE dispatch even ran -- so once
        that question inevitably fell through to the honest fallback (it
        was never actually about the category as a query), the NEXT
        follow-up ("any similar cases to this one") had already lost the
        order it needed, even though the intervening question never used
        it for anything. Context must only update when a real answer was
        actually found from the order/category it names."""
        _, ctx = qa.answer_with_context("what happened to order_2")
        self.assertEqual(ctx["last_order_id"], "order_2")

        result, ctx2 = qa.answer_with_context("duplicate transactions are so annoying honestly", ctx)
        self.assertEqual(result, qa.FALLBACK_MESSAGE)
        self.assertEqual(ctx2["last_order_id"], "order_2")

        result3, _ = qa.answer_with_context("are there any similar cases to this one", ctx2)
        self.assertIn("order_2", result3)

    def test_category_mention_that_is_actually_answered_does_update_context(self):
        """The counterpart to the regression above: when a category
        mention DOES lead to a real answer (not the fallback), it's
        legitimate for the conversation to have moved on -- last_order_id
        should update away, since the user genuinely just asked about a
        different category."""
        _, ctx = qa.answer_with_context("what happened to order_2")
        result, ctx2 = qa.answer_with_context("what about ON_HOLD_BY_RAZORPAY", ctx)
        self.assertNotEqual(result, qa.FALLBACK_MESSAGE)
        self.assertEqual(ctx2["last_category"], "ON_HOLD_BY_RAZORPAY")
        self.assertNotIn("last_order_id", ctx2)


if __name__ == "__main__":
    unittest.main()
