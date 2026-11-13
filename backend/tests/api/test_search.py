"""Hybrid search (GET /sessions/{id}/search) and rebuilding the index from Postgres.

The fake embedder gives every chunk and query the same meaning-vector, so meaning-search can't
tell chunks apart here: whatever ranks first is decided by the keyword half.
"""

import asyncio

import pytest
from qdrant_client import QdrantClient, models

from app.config import get_settings
from app.ingest.reindex import reindex_all
from tests.helpers import (
    FakeEmbedder,
    FakeQuestionCounter,
    FakeTopics,
    create_course,
    create_session,
    make_pdf,
    upload_ok,
)


@pytest.fixture
def session(client, login_as):
    login_as("alice")
    course = create_course(client)
    return create_session(client, course["id"])


def ingest(client, run_ingest, session_id, pages, filename="Lecture 5.pdf", kind=None, topics=None):
    document = upload_ok(client, session_id, filename=filename, content=make_pdf(pages), kind=kind)
    run_ingest(document["id"], topics=topics)
    return document


def search(client, session_id, **params) -> list[dict]:
    response = client.get(f"/sessions/{session_id}/search", params=params)
    assert response.status_code == 200, response.text
    return response.json()


# --- hybrid search --------------------------------------------------------------------


def test_exact_term_finds_the_right_page(client, session, run_ingest):
    ingest(client, run_ingest, session["id"], ["Lemma 3.1 about heaps", "Theorem 3.2 about graphs"])

    hits = search(client, session["id"], q="Theorem 3.2")

    assert hits[0]["page_number"] == 2
    assert "Theorem 3.2" in hits[0]["text"]
    assert client.app.state.embedder.queries == ["Theorem 3.2"]  # the query was embedded too


def test_hits_carry_citation_details(client, session, run_ingest):
    document = ingest(
        client, run_ingest, session["id"], ["Split brain happens when"], topics=FakeTopics([("Split Brain", 1, 1)])
    )

    [hit] = search(client, session["id"], q="split brain")

    assert hit["document_id"] == document["id"]
    assert hit["filename"] == "Lecture 5.pdf"
    assert hit["page_number"] == 1
    assert hit["topics"] == ["split brain"]
    assert hit["text"] == "Split brain happens when"


def test_search_never_leaves_the_users_session(client, session, run_ingest, login_as):
    mine = ingest(client, run_ingest, session["id"], ["Paxos consensus"])
    other_session = create_session(client, session["course_id"], name="Final prep")
    ingest(client, run_ingest, other_session["id"], ["Paxos consensus"])
    login_as("bob")
    bobs_session = create_session(client, create_course(client)["id"])
    ingest(client, run_ingest, bobs_session["id"], ["Paxos consensus"])

    login_as("alice")
    hits = search(client, session["id"], q="Paxos consensus", limit=20)
    assert [h["document_id"] for h in hits] == [mine["id"]]


def test_kind_filter(client, session, run_ingest):
    ingest(client, run_ingest, session["id"], ["Raft leader election"], filename="lecture.pdf")
    notes = ingest(client, run_ingest, session["id"], ["Raft leader election"], filename="notes.pdf", kind="notes")

    hits = search(client, session["id"], q="Raft", kind="notes")
    assert [h["document_id"] for h in hits] == [notes["id"]]


def test_topic_filter_ignores_case_and_punctuation(client, session, run_ingest):
    ingest(
        client,
        run_ingest,
        session["id"],
        ["Heaps and sift-up", "Graphs and BFS"],
        topics=FakeTopics([("Heaps", 1, 1), ("Graphs", 2, 2)]),
    )

    hits = search(client, session["id"], q="and", topic="graphs.")
    assert [h["page_number"] for h in hits] == [2]


def test_past_exams_never_show_up(client, session, run_ingest):
    exam = upload_ok(client, session["id"], content=make_pdf(["1. Explain Paxos"]), kind="past_exam")
    run_ingest(exam["id"], questions=FakeQuestionCounter([("1", "long_answer", 10)]))

    assert search(client, session["id"], q="Explain Paxos") == []


def test_invalid_searches_are_rejected(client, session, login_as):
    assert client.get(f"/sessions/{session['id']}/search", params={"q": ""}).status_code == 422
    assert client.get(f"/sessions/{session['id']}/search", params={"q": "x", "kind": "past_exam"}).status_code == 422
    login_as("bob")
    assert client.get(f"/sessions/{session['id']}/search", params={"q": "x"}).status_code == 404


# --- rebuilding the index from Postgres ---------------------------------------------------


def test_reindex_rebuilds_chunks_from_postgres(client, session, run_ingest, chunk_payloads):
    lecture = ingest(
        client, run_ingest, session["id"], ["Heaps intro", "Sift-up"], topics=FakeTopics([("Heaps", 1, 2)])
    )
    exam = upload_ok(client, session["id"], content=make_pdf(["1. Explain heaps"]), kind="past_exam")
    run_ingest(exam["id"], questions=FakeQuestionCounter([("1", "long_answer", 10)]))
    upload_ok(client, session["id"], content=make_pdf(["Not processed yet"]))  # still queued
    before = chunk_payloads(lecture["id"])

    embedder = FakeEmbedder(get_settings().embed_dimensions)
    result = asyncio.run(reindex_all(get_settings(), embedder=embedder))

    assert (result.documents, result.chunks) == (1, 2)  # only the ready lecture
    assert chunk_payloads(lecture["id"]) == before  # same text, pages and topics, from Postgres
    assert [title for _, title in embedder.calls] == ["Lecture 5"]  # only embeddings were redone

    # Every rebuilt point has both vectors: meaning and keywords.
    settings = get_settings()
    qdrant = QdrantClient(url=settings.qdrant_url)
    points, _ = qdrant.scroll(settings.collection_name, with_vectors=True, limit=10)
    qdrant.close()
    assert points and all(set(p.vector) == {"dense", "sparse"} for p in points)


def test_collection_uses_idf_for_keywords(client):
    settings = get_settings()
    qdrant = QdrantClient(url=settings.qdrant_url)
    info = qdrant.get_collection(settings.collection_name)
    qdrant.close()
    assert info.config.params.sparse_vectors["sparse"].modifier == models.Modifier.IDF
