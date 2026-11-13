"""OCR of scanned PDF pages inside the ingestion pipeline (fake OCR, so no Gemini calls)."""

import pytest

from app.config import get_settings
from app.ingest.parsers import parse_pdf
from tests.helpers import FakeOcr, create_course, create_session, make_pdf, unreadable_font, upload_ok


@pytest.fixture
def session(client, login_as):
    login_as("alice")
    course = create_course(client)
    return create_session(client, course["id"])


def page_row(db_fetch_one, document_id, number) -> dict:
    return db_fetch_one(
        "SELECT text, source FROM pages WHERE document_id = :id AND page_number = :n", id=document_id, n=number
    )


def document_status(client, session_id, document_id) -> dict:
    return next(d for d in client.get(f"/sessions/{session_id}/documents").json() if d["id"] == document_id)


TYPED_PAGE = "Heaps are complete binary trees stored in arrays"


def test_scanned_page_is_read_and_indexed(client, session, run_ingest, chunk_payloads, db_fetch_one, published):
    ocr = FakeOcr("Scanned notes: sift-up restores the heap property")
    document = upload_ok(client, session["id"], content=make_pdf([TYPED_PAGE, ""], image_pages=[2]))

    run_ingest(document["id"], ocr=ocr)

    # Gemini was asked about page 2 only, as a one-page PDF.
    assert len(ocr.pages) == 1
    assert len(parse_pdf(ocr.pages[0])) == 1

    assert page_row(db_fetch_one, document["id"], 1)["source"] == "text"
    scanned = page_row(db_fetch_one, document["id"], 2)
    assert scanned == {"text": "Scanned notes: sift-up restores the heap property", "source": "ocr"}

    payloads = chunk_payloads(document["id"])
    assert [p["page_number"] for p in payloads] == [1, 2]
    assert "sift-up" in payloads[1]["text"]

    stages = [(e["stage"], e.get("pages_done"), e.get("pages_total")) for e in published]
    assert stages[:3] == [("parsing", None, None), ("reading_scans", 0, 1), ("reading_scans", 1, 1)]
    assert stages[-1][0] == "ready"


def test_text_in_an_unreadable_font_is_read_with_ocr(client, session, run_ingest, chunk_payloads, db_fetch_one):
    slide = "Split Brain: the primary and backup disagree"
    ocr = FakeOcr(slide)
    document = upload_ok(client, session["id"], content=make_pdf([TYPED_PAGE, unreadable_font(slide)]))

    run_ingest(document["id"], ocr=ocr)

    assert len(ocr.pages) == 1  # only the scrambled page
    assert page_row(db_fetch_one, document["id"], 1)["source"] == "text"
    assert page_row(db_fetch_one, document["id"], 2) == {"text": slide, "source": "ocr"}
    assert chunk_payloads(document["id"])[1]["text"] == slide


def test_files_without_scans_skip_ocr(client, session, run_ingest, published):
    ocr = FakeOcr()
    document = upload_ok(client, session["id"], content=make_pdf([TYPED_PAGE, ""]))  # blank page, no image

    run_ingest(document["id"], ocr=ocr)

    assert ocr.pages == []
    assert "reading_scans" not in [e["stage"] for e in published]


def test_fully_scanned_file_becomes_ready(client, session, run_ingest):
    document = upload_ok(client, session["id"], content=make_pdf(["", "", ""], image_pages=[1, 2, 3]))
    run_ingest(document["id"], ocr=FakeOcr())
    assert document_status(client, session["id"], document["id"])["status"] == "ready"


def test_retry_reuses_pages_already_read(client, session, run_ingest, db_fetch_one):
    class BrokenEmbedder:
        async def embed_documents(self, texts, title):
            raise RuntimeError("Gemini is down")

    document = upload_ok(client, session["id"], content=make_pdf([TYPED_PAGE, ""], image_pages=[2]))
    first_ocr = FakeOcr("Read on the first attempt")
    with pytest.raises(RuntimeError):
        run_ingest(document["id"], embedder=BrokenEmbedder(), ocr=first_ocr)
    assert document_status(client, session["id"], document["id"])["status"] == "failed"
    assert len(first_ocr.pages) == 1

    client.post(f"/documents/{document['id']}/retry")
    second_ocr = FakeOcr("Would cost another call")
    run_ingest(document["id"], ocr=second_ocr)

    assert second_ocr.pages == []  # not paid for twice
    assert page_row(db_fetch_one, document["id"], 2)["text"] == "Read on the first attempt"
    assert document_status(client, session["id"], document["id"])["status"] == "ready"


def test_too_many_scanned_pages_fail_with_a_clear_message(client, session, run_ingest):
    limit = get_settings().max_ocr_pages
    ocr = FakeOcr()
    pdf = make_pdf([""] * (limit + 1), image_pages=range(1, limit + 2))
    document = upload_ok(client, session["id"], content=pdf)

    run_ingest(document["id"], ocr=ocr)

    result = document_status(client, session["id"], document["id"])
    assert result["status"] == "failed"
    assert f"{limit + 1} pages that must be read as images" in result["error"]
    assert f"at most {limit}" in result["error"]
    assert ocr.pages == []  # refused before any call
