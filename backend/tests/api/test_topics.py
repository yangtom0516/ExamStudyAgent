"""Topic extraction in the pipeline and GET /courses/{id}/topics (fake extractor, no Gemini calls)."""

import pytest

from app.ingest.pipeline import GENERIC_ERROR
from app.ingest.topics import TopicExtractionError
from tests.helpers import FakeTopics, create_course, create_session, make_pdf, upload_ok

THREE_PAGES = make_pdf(["Heaps are trees", "Sift-up and sift-down", "Graphs and BFS"])


@pytest.fixture
def course(client, login_as):
    login_as("alice")
    return create_course(client)


@pytest.fixture
def session(client, course):
    return create_session(client, course["id"])


def topics_of(client, course_id) -> list[dict]:
    response = client.get(f"/courses/{course_id}/topics")
    assert response.status_code == 200, response.text
    return response.json()


def summary(topics: list[dict]) -> list[tuple]:
    return [(t["name"], t["documents"], t["pages"]) for t in topics]


# --- pipeline ------------------------------------------------------------------------


def test_topics_are_saved_and_chunks_tagged(client, course, session, run_ingest, chunk_payloads, published):
    extractor = FakeTopics([("Binary Heaps", 1, 2), ("Graphs", 3, 3)])
    document = upload_ok(client, session["id"], filename="Lecture 5.pdf", content=THREE_PAGES)

    run_ingest(document["id"], topics=extractor)

    assert extractor.calls == [{"existing": [], "title": "Lecture 5", "pages": 3}]
    assert summary(topics_of(client, course["id"])) == [("Binary Heaps", 1, 2), ("Graphs", 1, 1)]
    assert [p["topics"] for p in chunk_payloads(document["id"])] == [["binary heaps"], ["binary heaps"], ["graphs"]]
    assert "finding_topics" in [e["stage"] for e in published]


def test_second_document_reuses_existing_topics(client, course, session, run_ingest):
    first = upload_ok(client, session["id"], filename="a.pdf", content=THREE_PAGES)
    run_ingest(first["id"], topics=FakeTopics([("Heaps", 1, 3)]))

    second_extractor = FakeTopics([("heaps.", 1, 1), ("Hashing", 2, 3)])  # "heaps." is the same topic
    second = upload_ok(client, session["id"], filename="b.pdf", content=THREE_PAGES)
    run_ingest(second["id"], topics=second_extractor)

    assert second_extractor.calls[0]["existing"] == ["Heaps"]  # the model was told about it
    assert summary(topics_of(client, course["id"])) == [("Hashing", 1, 2), ("Heaps", 2, 4)]


def test_topics_are_per_course(client, course, session, run_ingest):
    other_course = create_course(client, name="EECS 376")
    other_session = create_session(client, other_course["id"])
    for session_id in (session["id"], other_session["id"]):
        document = upload_ok(client, session_id, content=THREE_PAGES)
        run_ingest(document["id"], topics=FakeTopics([("Heaps", 1, 1)]))

    assert summary(topics_of(client, course["id"])) == [("Heaps", 1, 1)]
    assert summary(topics_of(client, other_course["id"])) == [("Heaps", 1, 1)]


def test_retry_reuses_topics_found_earlier(client, session, run_ingest, chunk_payloads):
    class BrokenEmbedder:
        async def embed_documents(self, texts, title):
            raise RuntimeError("Gemini is down")

    document = upload_ok(client, session["id"], content=THREE_PAGES)
    with pytest.raises(RuntimeError):
        run_ingest(document["id"], embedder=BrokenEmbedder(), topics=FakeTopics([("Heaps", 1, 3)]))

    client.post(f"/documents/{document['id']}/retry")
    second = FakeTopics([("Should not be asked", 1, 1)])
    run_ingest(document["id"], topics=second)

    assert second.calls == []  # no second (paid) call
    assert all(p["topics"] == ["heaps"] for p in chunk_payloads(document["id"]))


def test_unusable_topic_answer_fails_the_document(client, session, run_ingest):
    document = upload_ok(client, session["id"], content=THREE_PAGES)
    with pytest.raises(TopicExtractionError):
        run_ingest(document["id"], topics=FakeTopics(error=TopicExtractionError("not JSON")))

    result = client.get(f"/sessions/{session['id']}/documents").json()[0]
    assert result["status"] == "failed"
    assert result["error"] == GENERIC_ERROR


def test_file_without_text_fails_before_asking_for_topics(client, session, run_ingest):
    extractor = FakeTopics([("Heaps", 1, 1)])
    document = upload_ok(client, session["id"], content=make_pdf(["", ""]))
    run_ingest(document["id"], topics=extractor)
    assert extractor.calls == []


# --- GET /courses/{id}/topics ------------------------------------------------------------


def test_new_course_has_no_topics(client, course):
    assert topics_of(client, course["id"]) == []


def test_deleted_document_keeps_its_topics_with_zero_coverage(client, course, session, run_ingest):
    document = upload_ok(client, session["id"], content=THREE_PAGES)
    run_ingest(document["id"], topics=FakeTopics([("Heaps", 1, 2)]))

    client.delete(f"/documents/{document['id']}")
    assert summary(topics_of(client, course["id"])) == [("Heaps", 0, 0)]


def test_documents_still_processing_dont_count_yet(client, course, session, run_ingest, db_execute):
    document = upload_ok(client, session["id"], content=THREE_PAGES)
    run_ingest(document["id"], topics=FakeTopics([("Heaps", 1, 2)]))
    db_execute("UPDATE documents SET status = 'processing' WHERE id = :id", id=document["id"])

    assert summary(topics_of(client, course["id"])) == [("Heaps", 0, 0)]


def test_other_users_cannot_see_my_topics(client, course, login_as):
    login_as("bob")
    assert client.get(f"/courses/{course['id']}/topics").status_code == 404
