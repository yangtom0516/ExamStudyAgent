"""ExamWriter end to end through ADK, with a scripted model instead of Gemini (no API cost).

The script decides what the "model" asks for; everything else is real: ADK's loop, the tools,
the checks in services/exams.py, the test database and Qdrant. Whether a *real* model writes
*good* exams is checked by running it (4d) and by evals (step 8).
"""

import asyncio
import uuid

import pytest

from app.agents.exam_writer import CALL_LIMIT_MESSAGE, FAILED_MESSAGE, SAVED_ANYWAY, run_exam_writer
from app.agents.tools.context import Identity
from app.config import get_settings
from tests.helpers import (
    FakeTopics,
    ScriptedModel,
    call,
    create_course,
    create_session,
    make_pdf,
    open_agent_services,
    say,
    upload_ok,
)

REQUEST = "A short quiz on the view service"


@pytest.fixture
def alice(client, login_as, run_ingest) -> dict:
    """Alice's started session with a 3-page lecture, and her identity for the agent."""
    login_as("alice")
    session = create_session(client, create_course(client, "EECS 491")["id"])
    lecture = upload_ok(
        client,
        session["id"],
        filename="Lecture 5.pdf",
        content=make_pdf(["Split brain happens when", "The view service decides the primary", "Paxos"]),
    )
    run_ingest(lecture["id"], topics=FakeTopics([("Split Brain", 1, 1), ("View Service", 2, 2), ("Paxos", 3, 3)]))
    assert client.post(f"/sessions/{session['id']}/start").status_code == 200
    identity = Identity(
        user_id=uuid.UUID(client.get("/auth/me").json()["id"]),
        course_id=uuid.UUID(session["course_id"]),
        session_id=uuid.UUID(session["id"]),
    )
    return {"lecture": lecture["id"], "identity": identity}


@pytest.fixture
def run_agent(client):
    """run_agent(identity, script, ...) -> (ExamWriterRun, the ScriptedModel)."""

    def _run(identity, script, embedder=None, max_llm_calls=None, on_progress=None):
        model = ScriptedModel(script=script)

        async def _go():
            async with open_agent_services(embedder) as services:
                return await run_exam_writer(
                    services,
                    get_settings(),
                    identity=identity,
                    request=REQUEST,
                    model=model,
                    max_llm_calls=max_llm_calls,
                    on_progress=on_progress,
                )

        return asyncio.run(_go()), model

    return _run


def question(document_id: str, page: int = 2) -> dict:
    return {
        "type": "multiple_choice",
        "topic": "View Service",
        "points": 2,
        "prompt": "Who decides which server is the primary?",
        "choices": ["The client", "The view service"],
        "answer": "The view service",
        "sources": [{"document_id": document_id, "page": page}],
    }


def save(document_id: str, page: int = 2):
    return [call("save_exam", title="View Service Quiz", questions=[question(document_id, page)])]


def exam_count(db_fetch_one) -> int:
    return db_fetch_one("SELECT count(*) AS n FROM exams")["n"]


def test_agent_plans_searches_saves_and_replies(alice, run_agent, db_fetch_one):
    progress = []
    run, _ = run_agent(
        alice["identity"],
        [
            [call("get_exam_profile")],
            [call("search_course_materials", query="view service decides the primary")],
            save(alice["lecture"]),
            [say("Saved 'View Service Quiz': 1 multiple choice question on View Service, 2 points.")],
        ],
        on_progress=lambda step: progress.append((step.name, step.result is not None)),
    )

    assert run.error is None
    assert run.reply == "Saved 'View Service Quiz': 1 multiple choice question on View Service, 2 points."
    assert [step.name for step in run.steps] == ["get_exam_profile", "search_course_materials", "save_exam"]
    assert run.steps[1].result["results"][0]["page"] == 2  # the real search ran
    assert run.steps[2].result["ok"] is True

    row = db_fetch_one("SELECT title, request FROM exams WHERE id = :id", id=run.exam_id)
    assert row == {"title": "View Service Quiz", "request": REQUEST}

    # Each tool is reported twice: when asked for, and when its result is in.
    assert progress == [
        ("get_exam_profile", False),
        ("get_exam_profile", True),
        ("search_course_materials", False),
        ("search_course_materials", True),
        ("save_exam", False),
        ("save_exam", True),
    ]
    assert (run.llm_calls, run.input_tokens, run.output_tokens) == (4, 4000, 400)


def test_agent_sees_save_errors_and_can_fix_them(alice, run_agent, db_fetch_one):
    run, model = run_agent(
        alice["identity"],
        [save(alice["lecture"], page=9), save(alice["lecture"], page=2), [say("Saved.")]],
    )

    assert model.tool_results_seen(2) == [
        {"ok": False, "errors": ["q1: page 9 doesn't exist in Lecture 5.pdf (3 pages)"]}
    ]
    assert run.error is None and run.exam_id is not None
    assert exam_count(db_fetch_one) == 1  # the failed attempt saved nothing


def test_a_crashing_tool_does_not_end_the_run(alice, run_agent):
    class BrokenEmbedder:
        async def embed_query(self, query):
            raise RuntimeError("Gemini embedding is down")

    run, model = run_agent(
        alice["identity"],
        [[call("search_course_materials", query="view service")], [call("get_exam_profile")], [say("Try later.")]],
        embedder=BrokenEmbedder(),
    )

    [search_result] = model.tool_results_seen(2)
    assert search_result["ok"] is False
    assert search_result["error"].startswith("search_course_materials failed for a technical reason")
    assert model.tool_results_seen(3)[0]["ok"] is True  # other tools keep working
    assert run.error is None and run.reply == "Try later."


def test_call_limit_stops_a_runaway_agent(alice, run_agent, db_fetch_one):
    run, _ = run_agent(alice["identity"], [[call("search_course_materials", query="again")]], max_llm_calls=3)

    assert run.error == "call_limit"
    assert run.reply == CALL_LIMIT_MESSAGE
    assert run.llm_calls == 3
    assert run.exam_id is None and exam_count(db_fetch_one) == 0


def test_a_saved_exam_is_reported_even_if_the_run_fails_afterwards(alice, run_agent):
    run, _ = run_agent(alice["identity"], [save(alice["lecture"]), RuntimeError("Gemini is down")])

    assert run.error == "failed"
    assert run.exam_id is not None
    assert run.reply == FAILED_MESSAGE + SAVED_ANYWAY
