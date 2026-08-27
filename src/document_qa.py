"""
Grounds an uploaded document (a PDF or a photo/screenshot) in the same
persisted reconciliation data settlement_qa.py already answers questions
from. This module reads NO meaning into a document -- it extracts text,
finds order_id / settlement_id patterns using settlement_qa.py's own
regexes, and looks each one up through settlement_qa.answer() (the exact
same public, tested, deterministic path the chat widget already uses for
typed questions). It contributes zero new judgment about the
reconciliation data itself: OCR and PDF text extraction are both
fallible, so what this module produces is a QUERY, never an ANSWER -- the
answer still comes entirely from the persisted database via the existing
lookup, exactly like every other question in this chat.

Two extraction paths, both fully local -- no file, image, or extracted
text is ever sent anywhere over the network, the same "nothing leaves the
machine" property Pass 4's Ollama arbiter already has:
  - PDF: pypdf's own text-layer extraction. Works for any PDF with real,
    selectable text (an exported bank statement, say) -- not for a
    scanned page saved as a PDF with no text layer at all.
  - Image (PNG/JPG): OCR via pytesseract, which needs the Tesseract
    engine installed separately (see README) -- exactly the same kind of
    optional local dependency Ollama already is for the LLM arbiter. If
    either the Python package or the Tesseract binary isn't present,
    this returns an honest "OCR isn't available" message instead of
    silently producing nothing or guessing -- the same discipline
    llm_matcher.py already applies when Ollama isn't reachable.

Capped at MAX_IDS_LOOKED_UP found IDs per document so one statement
listing hundreds of orders can't flood the chat panel.
"""
import io

import pypdf

import settlement_qa

# pytesseract needs the separate Tesseract engine binary installed to
# actually work (see README) -- exactly the same kind of optional local
# dependency Ollama already is for the LLM arbiter. Imported at module
# level, guarded, so its absence is a normal, testable condition
# (pytesseract is None) rather than an exception raised deep inside a
# request handler.
try:
    import pytesseract
    from PIL import Image, UnidentifiedImageError
except ImportError:
    pytesseract = None
    Image = None
    UnidentifiedImageError = Exception

MAX_IDS_LOOKED_UP = 5
MAX_TEXT_PREVIEW_CHARS = 400  # how much of the extracted text to echo back when nothing was found, so the merchant can see what was actually read


def _extract_pdf_text(file_bytes: bytes) -> str:
    reader = pypdf.PdfReader(io.BytesIO(file_bytes))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _extract_image_text(file_bytes: bytes) -> str | None:
    """Returns None if OCR isn't available on this machine -- either the
    pytesseract package isn't installed, or it is but the separate
    Tesseract engine binary isn't -- distinct from an empty string
    (OCR ran fine but found no legible text). The caller must not treat
    these the same way."""
    if pytesseract is None:
        return None
    try:
        image = Image.open(io.BytesIO(file_bytes))
        return pytesseract.image_to_string(image)
    except pytesseract.TesseractNotFoundError:
        return None
    except UnidentifiedImageError:
        # Not a real/parseable image file (corrupt upload, wrong format
        # despite the extension) -- distinct from "OCR unavailable": the
        # infrastructure is fine, this particular file just isn't a
        # readable image. Treated the same as "OCR found no text" by the
        # caller, which is the honest outcome either way.
        return ""


def _find_referenced_ids(text: str) -> list[str]:
    """Every order_id / settlement_id mentioned in `text`, in the order
    they first appear, deduplicated -- reuses settlement_qa.py's own
    regexes rather than inventing a second pattern that could quietly
    drift out of sync with the one the chat's typed-question path uses."""
    order_ids = (f"order_{m.group(1)}" for m in settlement_qa.ORDER_ID_PATTERN.finditer(text))
    settlement_ids = (m.group(1) for m in settlement_qa.SETTLEMENT_ID_PATTERN.finditer(text))
    return list(dict.fromkeys(list(order_ids) + list(settlement_ids)))


def answer_about_document(filename: str, file_bytes: bytes, content_type: str) -> str:
    """Entry point: extracts text locally from the uploaded file, finds
    any order/settlement IDs in it, and answers about each one through
    settlement_qa.answer() -- the same grounded path typed questions use.
    Every branch is honest about what it couldn't do, rather than
    guessing: an unreadable scan, missing OCR, or a document with no
    reconciliation reference in it all say so plainly."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if content_type == "application/pdf" or ext == "pdf":
        text = _extract_pdf_text(file_bytes)
        if not text.strip():
            return (
                f'"{filename}" looks like a PDF with no selectable text -- probably a scanned '
                "page saved as a PDF. I can only read a PDF's real text layer today, not scanned "
                "images inside one. Try pasting the order or settlement ID directly instead."
            )
    elif ext in ("png", "jpg", "jpeg") or content_type.startswith("image/"):
        text = _extract_image_text(file_bytes)
        if text is None:
            return (
                "I can't read images on this machine yet -- OCR (Tesseract) isn't installed here. "
                "See the README for how to add it, or try uploading a text-based PDF instead, or "
                "paste the order or settlement ID directly."
            )
        if not text.strip():
            return f'I read "{filename}" but couldn\'t make out any legible text in it to search for an order or settlement.'
    else:
        return f'I can only read PDF or image files today, not "{filename}".'

    ids = _find_referenced_ids(text)[:MAX_IDS_LOOKED_UP]
    if not ids:
        preview = text.strip()[:MAX_TEXT_PREVIEW_CHARS]
        message = (
            f'I read "{filename}" but didn\'t find an order ID (like "order_1032") or a settlement '
            f'ID (like "setl_a1b2c3") anywhere in it, so there\'s nothing here to look up against '
            f"this run's data."
        )
        if preview:
            message += f"\n\nHere's what I could read from it:\n{preview}"
        return message

    lines = [f'Found {len(ids)} reference(s) in "{filename}":', ""]
    for identifier in ids:
        lines.append(f"{identifier}:")
        lines.append(settlement_qa.answer(f"what happened to {identifier}"))
        lines.append("")
    return "\n".join(lines).strip()
