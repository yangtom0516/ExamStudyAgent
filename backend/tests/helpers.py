"""Small helpers shared by tests."""

import io
from collections.abc import Iterable

from pptx import Presentation
from pptx.util import Inches

# Minimal files that pass the upload's "first bytes" check, for tests that never parse them.
PDF_BYTES = b"%PDF-1.4\n% tiny test file\n"
PPTX_BYTES = b"PK\x03\x04" + b"\x00" * 26


def make_pdf(page_texts: list[str], image_pages: Iterable[int] = ()) -> bytes:
    """A small but real PDF with one line of text per page. "" makes a page with no text.

    Pages listed in `image_pages` (1-based) also draw a full-page image, like a scan does.
    """
    font, image = 3, 4 + 2 * len(page_texts)  # object numbers
    image_pages = set(image_pages)
    bodies = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [%s] /Count %d >>"
        % (" ".join(f"{4 + 2 * i} 0 R" for i in range(len(page_texts))).encode(), len(page_texts)),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i, text in enumerate(page_texts):
        has_image = (i + 1) in image_pages
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = b"q 612 0 0 792 0 0 cm /Im1 Do Q\n" if has_image else b""
        if text:
            stream += f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1")
        xobjects = b" /XObject << /Im1 %d 0 R >>" % image if has_image else b""
        bodies.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 %d 0 R >>%s >> /Contents %d 0 R >>" % (font, xobjects, 5 + 2 * i)
        )
        bodies.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
    # A 1x1 grey pixel stands in for the scanned picture.
    bodies.append(
        b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray "
        b"/BitsPerComponent 8 /Length 1 >>\nstream\n\x80\nendstream"
    )

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(bodies, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref_at = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(bodies) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(bodies) + 1, xref_at)
    return bytes(out)


def unreadable_font(text: str) -> str:
    """`text` as it comes out of a PDF font without a letter table: every character shifted down by
    29, so "Split Brain" becomes "6SOLW\\x03%UDLQ". Put it in make_pdf to reproduce that bug."""
    return "".join(chr(ord(c) - 29) for c in text)


def make_encrypted_pdf(password: str = "secret") -> bytes:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(make_pdf(["Top secret notes"]))))
    writer.encrypt(user_password=password, algorithm="AES-256")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def make_pptx(slides: list[tuple[str, str, str]]) -> bytes:
    """A real PPTX. Each slide is (title, body, speaker notes); use "" to leave a part out."""
    presentation = Presentation()
    layout = presentation.slide_layouts[1]  # "Title and Content"
    for title, body, notes in slides:
        slide = presentation.slides.add_slide(layout)
        slide.shapes.title.text = title
        slide.placeholders[1].text = body
        if notes:
            slide.notes_slide.notes_text_frame.text = notes
    buffer = io.BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


def make_pptx_with_table(rows: list[list[str]]) -> bytes:
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])  # blank
    table = slide.shapes.add_table(len(rows), len(rows[0]), Inches(1), Inches(1), Inches(6), Inches(2)).table
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            table.cell(r, c).text = value
    buffer = io.BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


class FakeEmbedder:
    """Stands in for Gemini: returns a fixed vector per text and remembers what it was asked."""

    def __init__(self, dimensions: int) -> None:
        self.dimensions = dimensions
        self.calls: list[tuple[list[str], str]] = []
        self.queries: list[str] = []

    async def embed_documents(self, texts: list[str], title: str) -> list[list[float]]:
        self.calls.append((texts, title))
        return [[1.0] + [0.0] * (self.dimensions - 1) for _ in texts]

    async def embed_query(self, query: str) -> list[float]:
        # The same vector as every chunk: meaning-search can't tell chunks apart in tests, so
        # whatever ranks first is decided by the keyword half of hybrid search.
        self.queries.append(query)
        return [1.0] + [0.0] * (self.dimensions - 1)


def create_course(client, name="EECS 281") -> dict:
    response = client.post("/courses", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


def create_session(client, course_id, name="Midterm 1 prep") -> dict:
    response = client.post(f"/courses/{course_id}/sessions", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


def upload(client, session_id, filename="lecture1.pdf", content=PDF_BYTES, kind=None):
    """POST a file to a session; returns the raw response."""
    data = {"kind": kind} if kind else {}
    return client.post(
        f"/sessions/{session_id}/documents",
        files={"file": (filename, content, "application/octet-stream")},
        data=data,
    )


def upload_ok(client, session_id, **kwargs) -> dict:
    response = upload(client, session_id, **kwargs)
    assert response.status_code == 202, response.text
    return response.json()


class FakeOcr:
    """Stands in for Gemini OCR: "reads" every page as the same text and counts the calls."""

    def __init__(self, text: str = "Scanned notes about binary heaps") -> None:
        self.text = text
        self.pages: list[bytes] = []  # the one-page PDFs it was given

    async def read_page(self, page_pdf: bytes) -> str:
        self.pages.append(page_pdf)
        return self.text


class FakeTopics:
    """Stands in for Gemini topic extraction: returns fixed (name, page_start, page_end) topics
    and remembers what it was asked."""

    def __init__(self, topics: list[tuple[str, int, int]] = (), error: Exception | None = None) -> None:
        self.topics = list(topics)
        self.error = error
        self.calls: list[dict] = []

    async def extract(self, pages, existing, title):
        from app.ingest.topics import TopicRange

        self.calls.append({"existing": list(existing), "title": title, "pages": len(pages)})
        if self.error:
            raise self.error
        return [TopicRange(name=name, page_start=start, page_end=end) for name, start, end in self.topics]


class FakeQuestionCounter:
    """Stands in for Gemini question counting: returns fixed (number, type, points) questions
    and remembers each call."""

    def __init__(self, questions: list[tuple[str, str, int | None]] = (), error: Exception | None = None) -> None:
        self.questions = list(questions)
        self.error = error
        self.calls: list[dict] = []

    async def count(self, pages, title):
        from app.ingest.past_exams import QuestionItem

        self.calls.append({"title": title, "pages": len(pages)})
        if self.error:
            raise self.error
        return [QuestionItem(number=n, type=t, points=p) for n, t, p in self.questions]
