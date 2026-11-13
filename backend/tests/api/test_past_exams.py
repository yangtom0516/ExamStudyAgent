"""Past exams (structure only) and GET /sessions/{id}/exam-profile. Fakes instead of Gemini."""

import pytest

from app.config import get_settings
from app.ingest.past_exams import QuestionCountError
from app.ingest.pipeline import GENERIC_ERROR, NO_TEXT_ERROR
from tests.helpers import (
    FakeEmbedder,
    FakeQuestionCounter,
    FakeTopics,
    create_course,
    create_session,
    make_pdf,
    upload_ok,
)

EXAM_PDF = make_pdf(["Midterm. 1. Which of the following ... 2. Explain ...", "3. Prove that ..."])
MIDTERM = [("1", "multiple_choice", 2), ("2", "short_answer", 5), ("3", "long_answer", 10)]


@pytest.fixture
def session(client, login_as):
    login_as("alice")
    course = create_course(client)
    return create_session(client, course["id"])


def upload_exam(client, session_id, filename="Midterm 2025.pdf", content=EXAM_PDF):
    return upload_ok(client, session_id, filename=filename, content=content, kind="past_exam")


def stored_questions(db_fetch_all, document_id) -> list[tuple]:
    rows = db_fetch_all(
        "SELECT number, type, points FROM past_exam_questions WHERE document_id = :id ORDER BY position",
        id=document_id,
    )
    return [(r["number"], r["type"], r["points"]) for r in rows]


def document_status(client, session_id, document_id) -> dict:
    return next(d for d in client.get(f"/sessions/{session_id}/documents").json() if d["id"] == document_id)


def profile(client, session_id) -> dict:
    response = client.get(f"/sessions/{session_id}/exam-profile")
    assert response.status_code == 200, response.text
    return response.json()


# --- pipeline ---------------------------------------------------------------------------


def test_past_exam_stores_its_structure_only(
    client, session, run_ingest, chunk_payloads, db_fetch_all, published
):
    counter, topics, embedder = FakeQuestionCounter(MIDTERM), FakeTopics([("Heaps", 1, 2)]), FakeEmbedder(get_settings().embed_dimensions)
    document = upload_exam(client, session["id"])

    run_ingest(document["id"], questions=counter, topics=topics, embedder=embedder)

    assert counter.calls == [{"title": "Midterm 2025", "pages": 2}]
    assert stored_questions(db_fetch_all, document["id"]) == [
        ("1", "multiple_choice", 2),
        ("2", "short_answer", 5),
        ("3", "long_answer", 10),
    ]
    # Not chunked, embedded or topic-tagged: past exam content never reaches search.
    assert chunk_payloads(document["id"]) == []
    assert topics.calls == [] and embedder.calls == []

    result = document_status(client, session["id"], document["id"])
    assert result["status"] == "ready" and result["page_count"] == 2
    assert [e["stage"] for e in published] == ["parsing", "counting_questions", "ready"]
    assert published[-1]["question_count"] == 3


def test_lectures_never_use_the_question_counter(client, session, run_ingest):
    counter = FakeQuestionCounter(MIDTERM)
    document = upload_ok(client, session["id"], content=EXAM_PDF)  # kind = lecture
    run_ingest(document["id"], questions=counter)
    assert counter.calls == []


def test_exam_without_recognizable_questions_is_still_ready(client, session, run_ingest, db_fetch_all):
    document = upload_exam(client, session["id"])
    run_ingest(document["id"], questions=FakeQuestionCounter([]))

    assert document_status(client, session["id"], document["id"])["status"] == "ready"
    assert stored_questions(db_fetch_all, document["id"]) == []


def test_exam_without_any_text_fails_before_the_call(client, session, run_ingest):
    counter = FakeQuestionCounter(MIDTERM)
    document = upload_exam(client, session["id"], content=make_pdf(["", ""]))
    run_ingest(document["id"], questions=counter)

    result = document_status(client, session["id"], document["id"])
    assert result["status"] == "failed" and result["error"] == NO_TEXT_ERROR
    assert counter.calls == []


def test_unusable_answer_fails_the_document(client, session, run_ingest):
    document = upload_exam(client, session["id"])
    with pytest.raises(QuestionCountError):
        run_ingest(document["id"], questions=FakeQuestionCounter(error=QuestionCountError("not JSON")))

    result = document_status(client, session["id"], document["id"])
    assert result["status"] == "failed" and result["error"] == GENERIC_ERROR


def test_running_again_reuses_the_stored_structure(client, session, run_ingest, db_fetch_all, db_execute):
    document = upload_exam(client, session["id"])
    run_ingest(document["id"], questions=FakeQuestionCounter(MIDTERM))

    db_execute("UPDATE documents SET status = 'processing' WHERE id = :id", id=document["id"])  # redelivered job
    second = FakeQuestionCounter([("1", "other", None)])
    run_ingest(document["id"], questions=second)

    assert second.calls == []  # no second (paid) call
    assert len(stored_questions(db_fetch_all, document["id"])) == 3


def test_deleting_the_exam_removes_its_questions(client, session, run_ingest, db_fetch_all):
    document = upload_exam(client, session["id"])
    run_ingest(document["id"], questions=FakeQuestionCounter(MIDTERM))

    client.delete(f"/documents/{document['id']}")
    assert stored_questions(db_fetch_all, document["id"]) == []


# --- GET /sessions/{id}/exam-profile -----------------------------------------------------


def test_profile_combines_lecture_topics_and_past_exam_structure(client, session, run_ingest):
    lecture = upload_ok(client, session["id"], filename="Lecture 5.pdf", content=EXAM_PDF)
    run_ingest(lecture["id"], topics=FakeTopics([("Split Brain", 1, 2), ("View Service", 2, 2)]))
    for name, questions in [
        ("Midterm 2024.pdf", MIDTERM),
        ("Midterm 2025.pdf", [("1", "multiple_choice", 2), ("2", "multiple_choice", None), ("3", "other", 3)]),
    ]:
        exam = upload_exam(client, session["id"], filename=name)
        run_ingest(exam["id"], questions=FakeQuestionCounter(questions))

    result = profile(client, session["id"])

    assert result["topics"] == [
        {"name": "Split Brain", "documents": 1, "pages": 2},
        {"name": "View Service", "documents": 1, "pages": 1},
    ]
    assert result["past_exams"] == {
        "files": 2,
        "questions": {"multiple_choice": 3, "short_answer": 1, "long_answer": 1, "other": 1},
        "average_per_exam": {"multiple_choice": 1.5, "short_answer": 0.5, "long_answer": 0.5, "other": 0.5},
        "points": {"multiple_choice": 4, "short_answer": 5, "long_answer": 10, "other": 3},
        "average_points_per_exam": 11.0,
    }


def test_profile_without_past_exams_is_all_zeros(client, session):
    past_exams = profile(client, session["id"])["past_exams"]
    assert past_exams["files"] == 0
    assert set(past_exams["questions"].values()) == {0}
    assert past_exams["average_points_per_exam"] is None


def test_profile_ignores_other_sessions_and_unfinished_files(client, session, run_ingest):
    other_session = create_session(client, session["course_id"], name="Final prep")
    elsewhere = upload_ok(client, other_session["id"], content=EXAM_PDF)
    run_ingest(elsewhere["id"], topics=FakeTopics([("Paxos", 1, 1)]))
    upload_exam(client, session["id"])  # still queued: not counted yet

    result = profile(client, session["id"])
    assert result["topics"] == []
    assert result["past_exams"]["files"] == 0


def test_other_users_cannot_see_my_profile(client, session, login_as):
    login_as("bob")
    assert client.get(f"/sessions/{session['id']}/exam-profile").status_code == 404
