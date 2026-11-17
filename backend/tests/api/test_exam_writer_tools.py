"""ExamWriter's tools, called directly the way ADK will call them (no model, no API cost).

A fake ToolContext is just an object with a `state` dict: the identity the server would set.
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.agents.tools import make_exam_writer_tools
from app.agents.tools.context import LAST_EXAM_ID, initial_state
from app.agents.tools.materials import MAX_RESULTS, clip
from tests.helpers import (
    FakeQuestionCounter,
    FakeTopics,
    create_course,
    create_session,
    make_pdf,
    open_agent_services,
    upload_ok,
)

LECTURE_PAGES = ["Split brain happens when two primaries", "The view service decides the primary", "Paxos accept phase"]


@pytest.fixture
def call_tool(client):
    """call_tool(name, state, **arguments) -> the tool's result, like ADK running it."""

    def _call(name: str, state: dict, **arguments) -> dict:
        async def _go() -> dict:
            async with open_agent_services() as services:  # fresh connections: new event loop
                tools = {tool.__name__: tool for tool in make_exam_writer_tools(services)}
                return await tools[name](tool_context=SimpleNamespace(state=state), **arguments)

        return asyncio.run(_go())

    return _call


def state_for(client, session: dict, request: str = "5 questions on split brain") -> dict:
    """The identity the server would put in the ADK session state for the logged-in user."""
    user_id = client.get("/auth/me").json()["id"]
    return initial_state(user_id=user_id, course_id=session["course_id"], session_id=session["id"], request=request)


@pytest.fixture
def alice(client, login_as, run_ingest) -> dict:
    """Alice's started session: a 3-page lecture with topics, a notes file and a past exam."""
    login_as("alice")
    session = create_session(client, create_course(client, "EECS 491")["id"])
    lecture = upload_ok(client, session["id"], filename="Lecture 5.pdf", content=make_pdf(LECTURE_PAGES))
    run_ingest(lecture["id"], topics=FakeTopics([("Split Brain", 1, 1), ("View Service", 2, 2), ("Paxos", 3, 3)]))
    notes = upload_ok(client, session["id"], filename="notes.pdf", content=make_pdf(["My notes on the view service"]), kind="notes")
    run_ingest(notes["id"])
    exam = upload_ok(client, session["id"], filename="Midterm.pdf", content=make_pdf(["1. Q"]), kind="past_exam")
    run_ingest(exam["id"], questions=FakeQuestionCounter([("1", "multiple_choice", 2), ("2", "long_answer", 10)]))
    assert client.post(f"/sessions/{session['id']}/start").status_code == 200
    return {"session": session, "lecture": lecture["id"], "notes": notes["id"], "state": state_for(client, session)}


def mc_question(document_id: str, page: int = 2) -> dict:
    return {
        "type": "multiple_choice",
        "topic": "View Service",
        "points": 2,
        "prompt": "Who decides which server is the primary?",
        "choices": ["The client", "The view service"],
        "answer": "The view service",
        "sources": [{"document_id": document_id, "page": page}],
    }


# --- search_course_materials ---------------------------------------------------------------


def test_search_returns_passages_with_citations(alice, call_tool):
    result = call_tool("search_course_materials", alice["state"], query="view service decides")

    assert result["ok"] is True
    best = result["results"][0]
    assert best == {
        "document_id": alice["lecture"],
        "filename": "Lecture 5.pdf",
        "page": 2,
        "topics": ["view service"],
        "text": "The view service decides the primary",
    }


def test_search_returns_at_most_five_passages(client, alice, call_tool, run_ingest):
    session = create_session(client, alice["session"]["course_id"], name="Big session")
    lecture = upload_ok(client, session["id"], content=make_pdf([f"heap fact {i}" for i in range(1, 9)]))
    run_ingest(lecture["id"])

    result = call_tool("search_course_materials", state_for(client, session), query="heap fact")
    assert len(result["results"]) == MAX_RESULTS == 5


def test_search_never_leaves_the_users_session(client, alice, call_tool, run_ingest, login_as):
    login_as("bob")
    bobs_session = create_session(client, create_course(client)["id"])
    run_ingest(upload_ok(client, bobs_session["id"], content=make_pdf(["Split brain happens when"]))["id"])

    result = call_tool("search_course_materials", alice["state"], query="split brain", topic=None)
    assert result["results"] and {r["document_id"] for r in result["results"]} <= {alice["lecture"], alice["notes"]}


def test_search_filters_by_kind_and_topic(alice, call_tool):
    notes_only = call_tool("search_course_materials", alice["state"], query="view service", kind="notes")
    assert [r["document_id"] for r in notes_only["results"]] == [alice["notes"]]

    paxos_only = call_tool("search_course_materials", alice["state"], query="primary", topic="Paxos")
    assert [r["page"] for r in paxos_only["results"]] == [3]


def test_search_with_nothing_found_says_so(alice, call_tool):
    result = call_tool("search_course_materials", alice["state"], query="anything", topic="Heaps")
    assert result["ok"] is True and result["results"] == []
    assert "write fewer questions" in result["note"]


def test_search_rejects_bad_arguments(alice, call_tool):
    assert call_tool("search_course_materials", alice["state"], query="x", kind="past_exam")["ok"] is False
    assert call_tool("search_course_materials", alice["state"], query="   ")["ok"] is False


def test_long_passages_are_clipped():
    assert clip("short") == "short"
    assert clip("word " * 1000).endswith(" …") and len(clip("word " * 1000)) <= 1202


# --- get_exam_profile ------------------------------------------------------------------------


def test_profile_matches_the_api(client, alice, call_tool):
    result = call_tool("get_exam_profile", alice["state"])

    api = client.get(f"/sessions/{alice['session']['id']}/exam-profile").json()
    assert result == {"ok": True, **api}
    assert [t["name"] for t in result["topics"]] == ["Paxos", "Split Brain", "View Service"]
    assert result["past_exams"]["questions"]["long_answer"] == 1


def test_profile_of_someone_elses_session_is_refused(client, alice, call_tool, login_as):
    login_as("bob")
    bobs_state = state_for(client, alice["session"])  # bob's user id, alice's session
    assert call_tool("get_exam_profile", bobs_state) == {"ok": False, "error": "Session not found"}


# --- save_exam -----------------------------------------------------------------------------


def test_save_exam_saves_and_remembers_the_exam(client, alice, call_tool, db_fetch_one):
    state = alice["state"]
    result = call_tool("save_exam", state, title="Quick quiz", questions=[mc_question(alice["lecture"])])

    assert result == {"ok": True, "exam_id": result["exam_id"], "question_count": 1, "total_points": 2}
    assert state[LAST_EXAM_ID] == result["exam_id"]  # for the chat stream's exam_ready event (step 6)
    row = db_fetch_one("SELECT title, request FROM exams WHERE id = :id", id=result["exam_id"])
    assert row == {"title": "Quick quiz", "request": "5 questions on split brain"}  # request from the state
    assert client.get(f"/exams/{result['exam_id']}").status_code == 200


def test_save_exam_returns_errors_for_the_model_to_fix(alice, call_tool, db_fetch_one):
    state = alice["state"]
    result = call_tool("save_exam", state, title="Quiz", questions=[mc_question(alice["lecture"], page=9)])

    assert result == {"ok": False, "errors": ["q1: page 9 doesn't exist in Lecture 5.pdf (3 pages)"]}
    assert LAST_EXAM_ID not in state
    assert db_fetch_one("SELECT count(*) AS n FROM exams")["n"] == 0


# --- identity ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("get_exam_profile", {}),
        ("search_course_materials", {"query": "split brain"}),
        ("save_exam", {"title": "Quiz", "questions": []}),
    ],
)
def test_every_tool_refuses_without_identity(client, call_tool, name, arguments):
    result = call_tool(name, {}, **arguments)
    assert result["ok"] is False and "isn't linked to a study session" in result["error"]
