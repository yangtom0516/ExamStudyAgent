"""scripts/write_exam.py against the test database, with a scripted model instead of Gemini
(no API cost): picking a session, refusing ones that can't get an exam, and a whole run's output."""

import asyncio
import io
import uuid

import pytest

from app.config import get_settings
from scripts.write_exam import ScriptError, find_session, format_sessions, list_sessions, write_exam
from tests.helpers import (
    FakeQuestionCounter,
    FakeTopics,
    ScriptedModel,
    call,
    create_course,
    create_session,
    make_pdf,
    open_agent_services,
    say,
    unreadable_font,
    upload_ok,
)

LECTURE_PAGES = ["Split brain happens when", "The view service decides the primary", "Paxos accept phase"]


@pytest.fixture
def alice(client, login_as, run_ingest) -> dict:
    """Alice's course with a started session (lecture + past exam) and a session still in setup."""
    login_as("alice")
    course = create_course(client, "EECS 491")
    session = create_session(client, course["id"], name="Midterm prep")
    lecture = upload_ok(client, session["id"], filename="Lecture 5.pdf", content=make_pdf(LECTURE_PAGES))
    run_ingest(lecture["id"], topics=FakeTopics([("Split Brain", 1, 1), ("View Service", 2, 2), ("Paxos", 3, 3)]))
    exam = upload_ok(client, session["id"], filename="Midterm.pdf", content=make_pdf(["1. Q"]), kind="past_exam")
    run_ingest(exam["id"], questions=FakeQuestionCounter([("1", "multiple_choice", 2)]))
    assert client.post(f"/sessions/{session['id']}/start").status_code == 200
    draft = create_session(client, course["id"], name="Final prep")
    return {"course": course["id"], "session": session["id"], "lecture": lecture["id"], "draft": draft["id"]}


@pytest.fixture
def in_db(client):
    """in_db(fn) -> the result of `await fn(db)` on a test database session."""

    def _run(fn):
        async def _go():
            async with open_agent_services() as services, services.sessionmaker() as db:
                return await fn(db)

        return asyncio.run(_go())

    return _run


@pytest.fixture
def run_script(client):
    """run_script(session_id, script, max_llm_calls=25) -> (ExamWriterRun, printed text, model)."""

    def _run(session_id: str, script: list, max_llm_calls: int = 25):
        model = ScriptedModel(script=script)
        out = io.StringIO()

        async def _go():
            async with open_agent_services() as services:
                return await write_exam(
                    services,
                    get_settings(),
                    session_id=session_id,
                    request="A quiz on the view service",
                    model=model,
                    max_llm_calls=max_llm_calls,
                    out=out,
                )

        return asyncio.run(_go()), out.getvalue(), model

    return _run


def mc_question(document_id: str, page: int) -> dict:
    return {
        "type": "multiple_choice",
        "topic": "View Service",
        "points": 2,
        "prompt": "Who decides which server is the primary?",
        "choices": ["The client", "The view service"],
        "answer": "The view service",
        "sources": [{"document_id": document_id, "page": page}],
    }


# --- picking a session ------------------------------------------------------------------------


def test_listing_shows_each_session_and_flags_scrambled_pages(alice, in_db, db_execute):
    # A page stored before the step 3 font fix: its text is glyph numbers, not letters.
    db_execute(
        "UPDATE pages SET text = :text WHERE document_id = :id AND page_number = 1",
        text=unreadable_font("Split brain happens when"),
        id=alice["lecture"],
    )
    rows = {row.name: row for row in in_db(list_sessions)}

    started, draft = rows["Midterm prep"], rows["Final prep"]
    assert (started.username, started.course, started.status) == ("alice", "EECS 491", "active")
    assert (started.materials, started.past_exams) == (1, 1)
    assert started.warnings == "1 scrambled page in Lecture 5.pdf"
    assert (draft.status, draft.materials, draft.warnings) == ("setup", 0, "")

    table = "\n".join(format_sessions(list(rows.values())))
    assert alice["session"] in table and alice["draft"] in table  # full ids, to copy


def test_sessions_that_cant_get_an_exam_are_refused(client, alice, in_db, run_ingest):
    exams_only = create_session(client, alice["course"], name="Only old exams")
    exam = upload_ok(client, exams_only["id"], filename="Final.pdf", content=make_pdf(["1. Q"]), kind="past_exam")
    run_ingest(exam["id"])
    assert client.post(f"/sessions/{exams_only['id']}/start").status_code == 200

    for session_id, message in [
        ("not-an-id", "isn't a session id"),
        (str(uuid.uuid4()), "No study session has the id"),
        (alice["draft"], "hasn't been started"),
        (exams_only["id"], "no lecture slides or notes"),
    ]:
        with pytest.raises(ScriptError, match=message):
            in_db(lambda db, session_id=session_id: find_session(db, session_id))

    target = in_db(lambda db: find_session(db, alice["session"]))
    assert (target.course, target.session, str(target.identity.session_id)) == ("EECS 491", "Midterm prep", alice["session"])


def test_a_refused_session_never_reaches_the_model(alice):
    model = ScriptedModel(script=[[say("unused")]])

    async def _go():
        async with open_agent_services() as services:
            await write_exam(
                services,
                get_settings(),
                session_id=alice["draft"],
                request="A quiz",
                model=model,
                max_llm_calls=25,
                out=io.StringIO(),
            )

    with pytest.raises(ScriptError, match="hasn't been started"):
        asyncio.run(_go())
    assert model.requests == []  # nothing spent


# --- a whole run --------------------------------------------------------------------------------


def test_a_run_prints_each_step_the_reply_the_stats_and_the_answer_key(alice, run_script):
    lecture = alice["lecture"]
    run, text, _ = run_script(
        alice["session"],
        [
            [call("get_exam_profile")],
            [call("search_course_materials", query="view service decides the primary")],
            [call("save_exam", title="View Service Quiz", questions=[mc_question(lecture, page=9)])],
            [call("save_exam", title="View Service Quiz", questions=[mc_question(lecture, page=2)])],
            [say("Saved 'View Service Quiz': 1 multiple choice question on View Service.")],
        ],
    )
    assert run.exam_id is not None
    lines = text.splitlines()

    # Header, then each tool call and its result, numbered.
    assert lines[:3] == [
        "Course 'EECS 491', session 'Midterm prep'",
        "Request: A quiz on the view service",
        "Model scripted, at most 25 model calls",
    ]
    assert "[1] get_exam_profile()" in lines
    assert "[1] -> 3 topics: Paxos (1p), Split Brain (1p), View Service (1p)" in lines
    assert "        past exams: 1 file, on average 1 multiple choice" in lines
    assert '[2] search_course_materials(query="view service decides the primary")' in lines
    assert "[2] -> 3 passages: Lecture 5.pdf p.2, " in text  # best match first
    assert '[3] save_exam(title="View Service Quiz", 1 question)' in lines
    assert "[3] -> rejected, 1 problem:" in lines
    assert "        q1: page 9 doesn't exist in Lecture 5.pdf (3 pages)" in lines
    assert "[4] -> saved: 1 question, 2 points" in lines

    # The reply a student would see, and what the run cost.
    assert "Reply (what the student sees):\n  Saved 'View Service Quiz'" in text
    assert "scripted | 5 of 25 model calls | 5,000 input + 500 output tokens | cost unknown for this model" in text

    # The developer view: the answer key and the start of the cited page.
    assert "=== View Service Quiz: 1 question, 2 points ===" in lines
    assert "    [ ] a) The client" in lines
    assert "    [x] b) The view service" in lines
    assert '    source: Lecture 5.pdf p.2: "The view service decides the primary"' in lines


def test_a_run_that_saves_nothing_says_so(alice, run_script):
    run, text, _ = run_script(alice["session"], [[call("search_course_materials", query="again")]], max_llm_calls=2)

    assert run.error == "call_limit"
    assert "Reply (what the student sees):\n  I couldn't finish that; try a smaller request." in text
    assert "2 of 2 model calls" in text
    assert "Stopped early: call_limit" in text
    assert text.rstrip().endswith("No exam was saved.")
