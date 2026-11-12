"""The ingestion pipeline end to end (real Postgres, SeaweedFS and Qdrant; fake embedder instead of Gemini).

`run_ingest(document_id)` does what the Celery worker does when it picks up the job.
"""

import pytest

from app.config import get_settings
from app.ingest.pipeline import GENERIC_ERROR, NO_TEXT_ERROR
from tests.helpers import (
    FakeEmbedder,
    create_course,
    create_session,
    make_encrypted_pdf,
    make_pdf,
    make_pptx,
    upload_ok,
)


@pytest.fixture
def session(client, login_as):
    login_as("alice")
    course = create_course(client)
    return create_session(client, course["id"])


def get_document(client, session_id, document_id) -> dict:
    documents = client.get(f"/sessions/{session_id}/documents").json()
    return next(d for d in documents if d["id"] == document_id)


def fake_embedder() -> FakeEmbedder:
    return FakeEmbedder(get_settings().embed_dimensions)


def page_rows(db_fetch_one, document_id) -> int:
    return db_fetch_one("SELECT count(*) AS n FROM pages WHERE document_id = :id", id=document_id)["n"]


# --- happy paths ---------------------------------------------------------------


def test_upload_queues_an_ingestion_job(client, session, enqueued):
    document = upload_ok(client, session["id"], content=make_pdf(["Heaps"]))
    assert enqueued == [document["id"]]


def test_pdf_is_parsed_chunked_and_indexed(client, session, run_ingest, chunk_payloads, db_fetch_one):
    pdf = make_pdf(["Heaps are complete binary trees", "", "Dijkstra uses a priority queue"])
    document = upload_ok(client, session["id"], filename="Lecture 5.pdf", content=pdf)

    run_ingest(document["id"])

    result = get_document(client, session["id"], document["id"])
    assert result["status"] == "ready"
    assert result["error"] is None
    assert result["page_count"] == 3
    assert page_rows(db_fetch_one, document["id"]) == 3  # the empty page is kept too

    payloads = chunk_payloads(document["id"])
    assert [p["page_number"] for p in payloads] == [1, 3]  # nothing to index on page 2
    first = payloads[0]
    assert "Heaps are complete binary trees" in first["text"]
    assert first["session_id"] == session["id"]
    assert first["course_id"] == session["course_id"]
    assert first["kind"] == "lecture"
    assert first["user_id"]


def test_pptx_slides_are_indexed_with_their_notes(client, session, run_ingest, chunk_payloads):
    pptx = make_pptx([("Heaps", "Min-heap property", "Exam favourite"), ("Graphs", "BFS", "")])
    document = upload_ok(client, session["id"], filename="week2.pptx", content=pptx, kind="notes")

    run_ingest(document["id"])

    payloads = chunk_payloads(document["id"])
    assert [p["page_number"] for p in payloads] == [1, 2]
    assert "Exam favourite" in payloads[0]["text"]
    assert payloads[0]["kind"] == "notes"


def test_filename_is_given_to_the_embedder_as_title(client, session, run_ingest):
    embedder = fake_embedder()
    document = upload_ok(client, session["id"], filename="Lecture 5 - Heaps.pdf", content=make_pdf(["x"]))

    run_ingest(document["id"], embedder=embedder)

    assert embedder.calls[0][1] == "Lecture 5 - Heaps"


# --- running twice -------------------------------------------------------------


def test_running_twice_does_not_duplicate_anything(
    client, session, run_ingest, chunk_payloads, db_fetch_one, db_execute
):
    document = upload_ok(client, session["id"], content=make_pdf(["Heaps", "Graphs"]))
    run_ingest(document["id"])

    # Simulate a worker that crashed after finishing but before acknowledging: the job comes again.
    db_execute("UPDATE documents SET status = 'processing' WHERE id = :id", id=document["id"])
    run_ingest(document["id"])

    assert len(chunk_payloads(document["id"])) == 2
    assert page_rows(db_fetch_one, document["id"]) == 2
    assert get_document(client, session["id"], document["id"])["status"] == "ready"


def test_ready_documents_are_skipped(client, session, run_ingest):
    embedder = fake_embedder()
    document = upload_ok(client, session["id"], content=make_pdf(["Heaps"]))
    run_ingest(document["id"])

    run_ingest(document["id"], embedder=embedder)
    assert embedder.calls == []  # no repeated (paid) Gemini calls


def test_document_deleted_before_the_job_runs_is_skipped(client, session, run_ingest, chunk_payloads):
    document = upload_ok(client, session["id"], content=make_pdf(["Heaps"]))
    client.delete(f"/documents/{document['id']}")

    run_ingest(document["id"])  # must not raise
    assert chunk_payloads(document["id"]) == []


# --- failures ------------------------------------------------------------------


def test_password_protected_pdf_fails_with_a_clear_message(client, session, run_ingest, chunk_payloads):
    document = upload_ok(client, session["id"], content=make_encrypted_pdf())
    run_ingest(document["id"])

    result = get_document(client, session["id"], document["id"])
    assert result["status"] == "failed"
    assert "password-protected" in result["error"]
    assert chunk_payloads(document["id"]) == []


def test_file_with_no_text_fails(client, session, run_ingest):
    document = upload_ok(client, session["id"], content=make_pdf(["", ""]))
    run_ingest(document["id"])

    result = get_document(client, session["id"], document["id"])
    assert result["status"] == "failed"
    assert result["error"] == NO_TEXT_ERROR
    assert result["page_count"] == 2


def test_unexpected_error_marks_failed_and_is_raised(client, session, run_ingest):
    class BrokenEmbedder:
        async def embed_documents(self, texts, title):
            raise RuntimeError("Gemini is down")

    document = upload_ok(client, session["id"], content=make_pdf(["Heaps"]))
    with pytest.raises(RuntimeError):
        run_ingest(document["id"], embedder=BrokenEmbedder())

    result = get_document(client, session["id"], document["id"])
    assert result["status"] == "failed"
    assert result["error"] == GENERIC_ERROR


# --- retry -----------------------------------------------------------------------


def test_failed_document_can_be_retried(client, session, run_ingest, enqueued):
    document = upload_ok(client, session["id"], content=make_pdf(["", ""]))
    run_ingest(document["id"])
    enqueued.clear()

    response = client.post(f"/documents/{document['id']}/retry")
    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert response.json()["error"] is None
    assert enqueued == [document["id"]]


def test_only_failed_documents_can_be_retried(client, session):
    document = upload_ok(client, session["id"], content=make_pdf(["Heaps"]))  # still queued
    assert client.post(f"/documents/{document['id']}/retry").status_code == 409


def test_queue_down_marks_document_failed(client, session, monkeypatch):
    def broken_queue(document_id):
        raise ConnectionError("Redis is down")

    monkeypatch.setattr("app.api.documents.enqueue_ingest", broken_queue)
    document = upload_ok(client, session["id"], content=make_pdf(["Heaps"]))
    assert document["status"] == "failed"
    assert "Press Retry" in document["error"]


# --- cleanup ---------------------------------------------------------------------


def test_deleting_document_removes_its_chunks(client, session, run_ingest, chunk_payloads):
    document = upload_ok(client, session["id"], content=make_pdf(["Heaps"]))
    run_ingest(document["id"])
    assert chunk_payloads(document["id"])

    assert client.delete(f"/documents/{document['id']}").status_code == 204
    assert chunk_payloads(document["id"]) == []


def test_deleting_session_or_course_removes_chunks(client, session, run_ingest, chunk_payloads):
    other_session = create_session(client, session["course_id"], name="Final prep")
    first = upload_ok(client, session["id"], content=make_pdf(["Heaps"]))
    second = upload_ok(client, other_session["id"], content=make_pdf(["Graphs"]))
    run_ingest(first["id"])
    run_ingest(second["id"])

    client.delete(f"/sessions/{session['id']}")
    assert chunk_payloads(first["id"]) == []
    assert chunk_payloads(second["id"])  # other session untouched

    client.delete(f"/courses/{session['course_id']}")
    assert chunk_payloads(second["id"]) == []
