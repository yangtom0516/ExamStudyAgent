"""Turn an uploaded file into pages of plain text: one per PDF page, one per PowerPoint slide.

Files that can never be read (damaged, password-protected) raise UnreadableFileError, whose
message is shown to the user. Those fail at once; retrying wouldn't help.

Scanned PDF pages store a picture of the page, not text, so pypdf finds (almost) nothing on them.
Such pages are flagged `needs_ocr` and read by Gemini later (see ocr.py). So are pages whose text
comes out scrambled (see looks_garbled).
"""

import io
import re
from collections.abc import Iterable
from dataclasses import dataclass

from pptx import Presentation
from pptx.shapes.group import GroupShape
from pypdf import PdfReader, PdfWriter

# A page with less text than this *and* at least one image is treated as scanned. Not 0, because
# scans often still carry a stamped page number or header.
OCR_MAX_TEXT_CHARS = 20

# Some PDFs (often exported from PowerPoint) embed fonts without a table that says which letter
# each glyph is. pypdf then returns glyph numbers instead of letters, e.g. "Split Brain" comes out
# as "6SOLW\x03%UDLQ": every space becomes the invisible control character \x03. Real text has
# (almost) no control characters, so a page where more than this share of characters are control
# characters is treated as unreadable and sent to OCR, which reads the page as it looks.
GARBLED_MIN_SHARE = 0.05


class UnreadableFileError(Exception):
    """The file can't be read. The message is user-facing."""


@dataclass(frozen=True)
class ParsedPage:
    number: int  # 1-based
    text: str  # "" when the page has no text layer (e.g. a scan)
    needs_ocr: bool = False  # scanned (little text, but an image) or text that came out scrambled
    source: str = "text"  # "text" (from the file's text layer) or "ocr" (read by Gemini)


def clean_text(text: str) -> str:
    text = text.replace("\x00", "")  # Postgres can't store NUL characters
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)  # runs of spaces/tabs -> one space
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)  # at most one blank line in a row
    return text.strip()


def parse_document(data: bytes, extension: str) -> list[ParsedPage]:
    if extension == ".pdf":
        return parse_pdf(data)
    if extension == ".pptx":
        return parse_pptx(data)
    raise UnreadableFileError(f"Unsupported file type {extension}")


# --- PDF ---------------------------------------------------------------------


def _open_pdf(data: bytes) -> PdfReader:
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        # Some PDFs are "encrypted" with an empty password just to restrict printing; those open fine.
        try:
            opened = reader.decrypt("")
        except Exception:
            opened = 0
        if not opened:
            raise UnreadableFileError("This PDF is password-protected. Remove the password and upload it again.")
    return reader


def looks_garbled(text: str) -> bool:
    """Whether extracted text is scrambled glyph numbers rather than letters (see GARBLED_MIN_SHARE)."""
    controls = sum(1 for c in text if c < " " and c != "\n")
    return controls > 0 and controls / len(text) > GARBLED_MIN_SHARE


def _has_images(page) -> bool:
    """Whether the page draws any image. Counting doesn't decode the pixels, so it's cheap."""
    try:
        return len(page.images) > 0
    except Exception:
        return False


def parse_pdf(data: bytes) -> list[ParsedPage]:
    try:
        reader = _open_pdf(data)
        pages = []
        for number, page in enumerate(reader.pages, start=1):
            text = clean_text(page.extract_text() or "")
            if looks_garbled(text):
                pages.append(ParsedPage(number, "", needs_ocr=True))  # never keep the scrambled text
                continue
            scanned = len(text) < OCR_MAX_TEXT_CHARS and _has_images(page)
            pages.append(ParsedPage(number, text, needs_ocr=scanned))
    except UnreadableFileError:
        raise
    except Exception as exc:
        raise UnreadableFileError("This PDF is damaged or not a valid PDF.") from exc
    if not pages:
        raise UnreadableFileError("This PDF is damaged or has no pages.")
    return pages


def split_pdf_pages(data: bytes, numbers: Iterable[int]) -> dict[int, bytes]:
    """Copy each requested page (1-based) into its own one-page PDF, e.g. to send it for OCR."""
    reader = _open_pdf(data)
    result = {}
    for number in numbers:
        writer = PdfWriter()
        writer.add_page(reader.pages[number - 1])
        buffer = io.BytesIO()
        writer.write(buffer)
        result[number] = buffer.getvalue()
    return result


# --- PowerPoint ----------------------------------------------------------------


def _shape_texts(shapes):
    """Text of every shape on a slide, including shapes inside groups and table cells."""
    for shape in shapes:
        if isinstance(shape, GroupShape):
            yield from _shape_texts(shape.shapes)
        elif shape.has_text_frame:
            yield shape.text_frame.text
        elif shape.has_table:
            for row in shape.table.rows:
                yield " | ".join(cell.text for cell in row.cells)


def _slide_text(slide) -> str:
    text = "\n".join(t for t in _shape_texts(slide.shapes) if t.strip())
    if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
        notes = slide.notes_slide.notes_text_frame.text.strip()
        if notes:
            text += f"\n\nSpeaker notes:\n{notes}"
    return clean_text(text)


def parse_pptx(data: bytes) -> list[ParsedPage]:
    try:
        presentation = Presentation(io.BytesIO(data))
        pages = [
            ParsedPage(number, _slide_text(slide))
            for number, slide in enumerate(presentation.slides, start=1)
        ]
    except Exception as exc:
        # Also catches other ZIP-based files renamed to .pptx (e.g. a .docx).
        raise UnreadableFileError("This PowerPoint file is damaged or not a valid PPTX.") from exc
    if not pages:
        raise UnreadableFileError("This presentation has no slides.")
    return pages
