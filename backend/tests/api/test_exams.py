"""Saving exams (the checks against the database) and reading them through the API.

Exams are saved with the `save_exam` fixture, the same service ExamWriter's tool will call.
"""

import pytest

from tests.helpers import FakeQuestionCounter, FakeTopics, create_course, create_session, make_pdf, upload_ok


@pytest.fixture
def setup(client, login_as, run_ingest) -> dict:
    """Alice's started session: a 3-page lecture (topics Split Brain, View Service) and a past exam.
    Plus a second session in the same course with its own lecture."""
    login_as("alice")
    course = create_course(client, "EECS 491")
    session = create_session(client, course["id"])
    lecture = upload_ok(
        client, session["id"], filename="Lecture 5.pdf", content=make_pdf(["Split brain", "View service", "More"])
    )
    run_ingest(lecture["id"], topics=FakeTopics([("Split Brain", 1, 2), ("View Service", 2, 3)]))
    past_exam = upload_ok(client, session["id"], filename="Midterm.pdf", content=make_pdf(["1. Q"]), kind="past_exam")
    run_ingest(past_exam["id"], questions=FakeQuestionCounter([("1", "short_answer", 5)]))
    assert client.post(f"/sessions/{session['id']}/start").status_code == 200

    other_session = create_session(client, course["id"], name="Final prep")
    other_lecture = upload_ok(client, other_session["id"], content=make_pdf(["Paxos"]))
    run_ingest(other_lecture["id"])
    return {
        "session": session,
        "lecture": lecture["id"],
        "past_exam": past_exam["id"],
        "other_session": other_session,
        "other_lecture": other_lecture["id"],
    }


def mc(document_id, page=1, **overrides) -> dict:
    question = {
        "type": "multiple_choice",
        "topic": "Split Brain",
        "points": 2,
        "prompt": "What prevents split brain?",
        "choices": ["Client retries", "The backup confirms the current view"],
        "answer": "The backup confirms the current view",
        "sources": [{"document_id": document_id, "page": page}],
    }
    return question | overrides


def written(document_id, page=2, **overrides) -> dict:
    question = {
        "type": "short_answer",
        "topic": "View Service",
        "points": 5,
        "prompt": "Why must the backup know if the view changed?",
        "answer": "A view change means the primary was declared dead.",
        "rubric": ["Primary declared dead", "Backup gets promoted"],
        "sources": [{"document_id": document_id, "page": page}],
    }
    return question | overrides


def exam(*questions, title="Practice midterm") -> dict:
    return {"title": title, "questions": list(questions)}


def exam_count(db_fetch_one) -> int:
    return db_fetch_one("SELECT count(*) AS n FROM exams")["n"]


# --- saving -----------------------------------------------------------------------------


def test_valid_exam_is_saved(setup, save_exam, db_fetch_one):
    lecture = setup["lecture"]
    result = save_exam(
        setup["session"]["id"],
        exam(mc(lecture, topic="split brain"), written(lecture)),
        request="2 questions on split brain",
    )

    assert result.ok and result.errors == []
    row = db_fetch_one("SELECT title, request, questions FROM exams WHERE id = :id", id=result.exam_id)
    assert row["title"] == "Practice midterm"
    assert row["request"] == "2 questions on split brain"
    first, second = row["questions"]
    assert [first["id"], second["id"]] == ["q1", "q2"]  # numbered by the server
    assert first["topic"] == "Split Brain"  # saved under the course's spelling
    assert first["answer"] == "The backup confirms the current view"  # answer key is stored
    assert second["rubric"] == ["Primary declared dead", "Backup gets promoted"]
    assert second["sources"] == [{"document_id": lecture, "page": 2}]


def test_sources_must_be_real_pages_of_this_sessions_lectures(setup, save_exam, db_fetch_one):
    missing = "00000000-0000-0000-0000-000000000000"
    result = save_exam(
        setup["session"]["id"],
        exam(
            mc(setup["lecture"], page=99),
            mc(setup["past_exam"]),
            mc(setup["other_lecture"]),
            mc(missing),
        ),
    )

    assert not result.ok
    assert result.errors == [
        "q1: page 99 doesn't exist in Lecture 5.pdf (3 pages)",
        f"q2: source {setup['past_exam']} is not a lecture or notes file in this session",
        f"q3: source {setup['other_lecture']} is not a lecture or notes file in this session",
        f"q4: source {missing} is not a lecture or notes file in this session",
    ]
    assert exam_count(db_fetch_one) == 0  # nothing saved


def test_topics_must_be_course_topics(setup, save_exam):
    result = save_exam(setup["session"]["id"], exam(mc(setup["lecture"], topic="Heaps")))
    assert result.errors == [
        "q1: unknown topic 'Heaps'",
        "Use one of the course topics: Split Brain, View Service",
    ]


def test_shape_errors_are_reported_before_the_database_checks(setup, save_exam, db_fetch_one):
    lecture = setup["lecture"]
    result = save_exam(setup["session"]["id"], exam(mc(lecture), mc(lecture, answer="Clocks")))
    assert result.errors == ["q2: answer must be exactly one of the choices (copy the choice's text)"]
    assert exam_count(db_fetch_one) == 0


def test_exams_need_a_started_session(setup, save_exam):
    result = save_exam(setup["other_session"]["id"], exam(mc(setup["other_lecture"])))
    assert result.errors == ["Exams can only be saved in a started session"]


def test_cannot_save_into_someone_elses_session(setup, save_exam, login_as):
    login_as("bob")
    result = save_exam(setup["session"]["id"], exam(mc(setup["lecture"])))
    assert result.errors == ["Session not found"]


# --- reading ----------------------------------------------------------------------------


def test_student_never_sees_the_answer_key(client, setup, save_exam):
    lecture = setup["lecture"]
    result = save_exam(setup["session"]["id"], exam(mc(lecture), written(lecture)), request="mixed exam")

    response = client.get(f"/exams/{result.exam_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Practice midterm" and body["request"] == "mixed exam"
    for question in body["questions"]:
        assert set(question) == {"id", "type", "topic", "points", "prompt", "choices"}
    assert body["questions"][0]["choices"] == ["Client retries", "The backup confirms the current view"]
    assert body["questions"][1]["choices"] == []
    assert "The backup confirms the current view" not in str(body["questions"][1])
    assert "rubric" not in response.text and "Primary declared dead" not in response.text


def test_list_exams_newest_first(client, setup, save_exam):
    lecture = setup["lecture"]
    save_exam(setup["session"]["id"], exam(mc(lecture), title="First"))
    save_exam(setup["session"]["id"], exam(mc(lecture), written(lecture), title="Second"))

    response = client.get(f"/sessions/{setup['session']['id']}/exams")

    assert response.status_code == 200
    assert [(e["title"], e["question_count"], e["total_points"]) for e in response.json()] == [
        ("Second", 2, 7),
        ("First", 1, 2),
    ]


def test_other_users_cannot_see_my_exams(client, setup, save_exam, login_as):
    result = save_exam(setup["session"]["id"], exam(mc(setup["lecture"])))
    login_as("bob")
    assert client.get(f"/exams/{result.exam_id}").status_code == 404
    assert client.get(f"/sessions/{setup['session']['id']}/exams").status_code == 404


def test_unknown_exam_is_404(client, setup):
    assert client.get("/exams/00000000-0000-0000-0000-000000000000").status_code == 404


def test_deleting_the_session_deletes_its_exams(client, setup, save_exam, db_fetch_one):
    save_exam(setup["session"]["id"], exam(mc(setup["lecture"])))
    assert client.delete(f"/sessions/{setup['session']['id']}").status_code == 204
    assert exam_count(db_fetch_one) == 0
