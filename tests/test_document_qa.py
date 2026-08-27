"""
Unit tests for document_qa.py. Mocks OCR/PDF extraction at the module
level (document_qa.pypdf / document_qa.pytesseract) where a real file
isn't the point of the test -- mirrors how test_llm_matcher.py and
test_qa_intent_router.py avoid depending on a real external model/engine
for logic-only tests, while still exercising a genuine pypdf round trip
for the PDF path, since pypdf is an always-present dependency here, not
an optional one.
"""
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import db  # noqa: E402
import document_qa as dq  # noqa: E402


def make_result(order_id, status, category=None, narration="", net=100.0):
    return {
        "order_id": order_id, "settlement_id": f"setl_{order_id}", "net": net,
        "match_key": f"settlement:setl_{order_id}",
        "status": status, "category": category, "reason": f"test reason for {order_id}",
        "narration": narration, "stage": ["test stage"],
    }


class TestFindReferencedIds(unittest.TestCase):
    def test_finds_order_and_settlement_ids(self):
        text = "Please check order_1032 and setl_abc123 for us."
        self.assertEqual(dq._find_referenced_ids(text), ["order_1032", "setl_abc123"])

    def test_finds_spaced_order_form(self):
        self.assertEqual(dq._find_referenced_ids("re: order 55, thanks"), ["order_55"])

    def test_deduplicates_preserving_first_appearance_order(self):
        text = "order_9 came up twice: once here, order_9, and setl_zzz once."
        self.assertEqual(dq._find_referenced_ids(text), ["order_9", "setl_zzz"])

    def test_no_ids_returns_empty_list(self):
        self.assertEqual(dq._find_referenced_ids("nothing relevant in this sentence"), [])


class TestExtractPdfText(unittest.TestCase):
    def test_blank_pdf_yields_empty_text(self):
        """A genuine round trip through the real pypdf dependency, not a
        mock -- a PDF with a blank page (no text layer) must yield an
        empty string, not raise."""
        pdf_bytes = blank_pdf_bytes()
        self.assertEqual(dq._extract_pdf_text(pdf_bytes).strip(), "")

    def test_extraction_uses_the_real_pypdf_reader(self):
        """Mocks pypdf itself only to verify _extract_pdf_text actually
        calls PdfReader().pages[*].extract_text() and joins the results
        -- the wiring, not pypdf's own correctness (covered above)."""
        fake_page1 = MagicMock()
        fake_page1.extract_text.return_value = "order_42 settled"
        fake_page2 = MagicMock()
        fake_page2.extract_text.return_value = None  # pypdf can return None for an unreadable page
        fake_reader = MagicMock()
        fake_reader.pages = [fake_page1, fake_page2]
        with patch.object(dq.pypdf, "PdfReader", return_value=fake_reader):
            result = dq._extract_pdf_text(b"irrelevant, PdfReader is mocked")
        self.assertEqual(result, "order_42 settled\n")


def blank_pdf_bytes() -> bytes:
    import pypdf
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


class TestExtractImageText(unittest.TestCase):
    def test_returns_none_when_pytesseract_not_installed(self):
        with patch.object(dq, "pytesseract", None):
            self.assertIsNone(dq._extract_image_text(b"anything"))

    def test_returns_none_when_tesseract_binary_missing(self):
        """pytesseract package present, but the separate Tesseract engine
        binary isn't -- a different, real failure mode from the package
        not being installed at all, and one this project already
        documents as an optional local dependency (same as Ollama)."""
        fake_pytesseract = MagicMock()
        fake_pytesseract.TesseractNotFoundError = RuntimeError
        fake_pytesseract.image_to_string.side_effect = RuntimeError("tesseract not found")
        fake_image_module = MagicMock()
        fake_image_module.open.return_value = "a real-looking image object"
        with patch.object(dq, "pytesseract", fake_pytesseract), patch.object(dq, "Image", fake_image_module):
            self.assertIsNone(dq._extract_image_text(b"a real png"))

    def test_corrupt_file_is_not_available_as_ocr_infra_failure(self):
        """A file that isn't a real image (wrong format, truncated
        upload) is a different case from OCR being unavailable -- the
        infrastructure is fine, this file just isn't readable. Must not
        be reported as "OCR isn't available", which would be misleading
        about the actual problem."""
        if dq.pytesseract is None:
            self.skipTest("pytesseract not installed in this environment")
        result = dq._extract_image_text(b"not a real image at all")
        self.assertEqual(result, "")


class TestAnswerAboutDocument(unittest.TestCase):
    """Integration-style: seeds real data and checks the full path from
    extracted text to the real settlement_qa.answer() lookup, mocking
    only the extraction step (not the lookup)."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = Path(self._tmpdir.name) / "test_reconcile.db"
        db.persist_results([
            make_result("order_2", "EXCEPTION", category="DUPLICATE"),
        ], run_id="run-1")

    def tearDown(self):
        db.DB_PATH = self._original_db_path
        self._tmpdir.cleanup()

    def test_pdf_with_a_known_order_id_gets_the_real_answer(self):
        with patch.object(dq, "_extract_pdf_text", return_value="Statement mentions order_2 as settled."):
            result = dq.answer_about_document("statement.pdf", b"fake pdf bytes", "application/pdf")
        self.assertIn("order_2", result)
        self.assertIn("DUPLICATE", result)
        self.assertIn("test reason for order_2", result)

    def test_pdf_with_no_selectable_text_says_so_honestly(self):
        with patch.object(dq, "_extract_pdf_text", return_value=""):
            result = dq.answer_about_document("scanned.pdf", b"fake pdf bytes", "application/pdf")
        self.assertIn("no selectable text", result)

    def test_pdf_with_no_ids_is_honest_not_fabricated(self):
        with patch.object(dq, "_extract_pdf_text", return_value="Just a generic cover letter, no references."):
            result = dq.answer_about_document("letter.pdf", b"fake pdf bytes", "application/pdf")
        self.assertIn("didn't find an order ID", result)
        self.assertIn("generic cover letter", result)  # echoes back what it actually read

    def test_image_ocr_unavailable_says_so_honestly(self):
        with patch.object(dq, "_extract_image_text", return_value=None):
            result = dq.answer_about_document("photo.png", b"fake image bytes", "image/png")
        self.assertIn("OCR (Tesseract) isn't installed", result)

    def test_image_with_no_legible_text_says_so_honestly(self):
        with patch.object(dq, "_extract_image_text", return_value="   "):
            result = dq.answer_about_document("blurry.jpg", b"fake image bytes", "image/jpeg")
        self.assertIn("couldn't make out", result)

    def test_image_with_a_known_order_id_gets_the_real_answer(self):
        with patch.object(dq, "_extract_image_text", return_value="order_2 -- Rs.100"):
            result = dq.answer_about_document("photo.png", b"fake image bytes", "image/png")
        self.assertIn("DUPLICATE", result)

    def test_unsupported_file_type_is_rejected_honestly(self):
        result = dq.answer_about_document("notes.docx", b"irrelevant", "application/vnd.openxmlformats")
        self.assertIn("can only read PDF or image files", result)

    def test_multiple_ids_are_capped(self):
        many_ids = " ".join(f"order_{i}" for i in range(20))
        with patch.object(dq, "_extract_pdf_text", return_value=many_ids):
            result = dq.answer_about_document("big.pdf", b"fake pdf bytes", "application/pdf")
        self.assertIn(f"Found {dq.MAX_IDS_LOOKED_UP} reference(s)", result)

    def test_unknown_order_in_document_is_honest_not_fabricated(self):
        with patch.object(dq, "_extract_pdf_text", return_value="order_999 was here"):
            result = dq.answer_about_document("statement.pdf", b"fake pdf bytes", "application/pdf")
        self.assertIn("No record of order_999", result)


if __name__ == "__main__":
    unittest.main()
