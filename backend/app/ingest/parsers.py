"""Turn an uploaded file into pages of plain text: one per PDF page, one per PowerPoint slide.

Files that can never be read (damaged, password-protected) raise UnreadableFileError, whose
message is shown to the user. Those fail at once; retrying wouldn't help.
"""

import io
import re
from dataclasses import dataclass

from pptx import Presentation
from pptx.shapes.group import GroupShape
from pypdf import PdfReader


class UnreadableFileError(Exception):
    """The file can't be read. The message is user-facing."""


@dataclass(frozen=True)
class ParsedPage:
    number: int  # 1-based
    text: str  # "" when the page has no text layer (e.g. a scan)


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


def parse_pdf(data: bytes) -> list[ParsedPage]:
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            # Some PDFs are "encrypted" with an empty password just to restrict printing; those open fine.
            try:
                opened = reader.decrypt("")
            except Exception:
                opened = 0
            if not opened:
                raise UnreadableFileError(
                    "This PDF is password-protected. Remove the password and upload it again."
                )
        pages = [
            ParsedPage(number, clean_text(page.extract_text() or ""))
            for number, page in enumerate(reader.pages, start=1)
        ]
    except UnreadableFileError:
        raise
    except Exception as exc:
        raise UnreadableFileError("This PDF is damaged or not a valid PDF.") from exc
    if not pages:
        raise UnreadableFileError("This PDF is damaged or has no pages.")
    return pages


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
