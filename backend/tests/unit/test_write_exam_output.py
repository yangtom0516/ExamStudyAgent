"""What scripts/write_exam.py prints, and the checks it makes before spending anything
(no database, model or API calls)."""

import io
from datetime import date

import pytest
from pydantic import SecretStr

from app.agents.exam_writer import ExamWriterRun, ToolStep
from app.config import get_settings
from scripts.write_exam import (
    ExamView,
    ProgressPrinter,
    ScriptError,
    block,
    describe_args,
    describe_result,
    estimate_cost,
    format_exam,
    format_stats,
    load_model,
)
from tests.helpers import unreadable_font

DOC = "11111111-1111-1111-1111-111111111111"


# --- cost -------------------------------------------------------------------------------------


def test_cost_uses_the_price_of_the_day():
    assert estimate_cost("gemini-3.8-flash", 1_000_000, 1_000_000, today=date(2026, 11, 18)) == pytest.approx(4.50)
    assert estimate_cost("gemini-3.8-flash", 1_000_000, 1_000_000, today=date(2027, 1, 1)) == pytest.approx(9.00)
    assert estimate_cost("gemini-3.5-flash-lite", 2_000_000, 0) == pytest.approx(0.60)
    assert estimate_cost("openrouter/openai/gpt-oss-120b:free", 1000, 1000) is None


def test_stats_line():
    run = ExamWriterRun(llm_calls=6, input_tokens=41_200, output_tokens=3_100)
    assert format_stats(run, "gemini-3.8-flash", 25, 38.2, today=date(2026, 11, 18)) == (
        "gemini-3.8-flash | 6 of 25 model calls | 41,200 input + 3,100 output tokens | about $0.043 | 38 s"
    )
    assert "cost unknown for this model" in format_stats(run, "scripted", 25, 1)


# --- checks before a run ----------------------------------------------------------------------


def test_missing_api_key_stops_before_a_model_is_made():
    settings = get_settings().model_copy(update={"google_api_key": SecretStr("")})
    with pytest.raises(ScriptError, match="GOOGLE_API_KEY is not set"):
        load_model("gemini-3.8-flash", settings)


def test_model_setup_problems_become_messages():
    settings = get_settings().model_copy(update={"google_api_key": SecretStr("test-key")})
    with pytest.raises(ScriptError, match="No agent model configured"):
        load_model("", settings)
    assert load_model("gemini-3.5-flash-lite", settings).model == "gemini-3.5-flash-lite"  # --model


# --- live progress ----------------------------------------------------------------------------


def test_tool_calls_fit_on_one_line():
    assert describe_args({}) == ""
    assert describe_args({"query": "split brain", "kind": None, "topic": "Split Brain"}) == (
        'query="split brain", topic="Split Brain"'
    )
    assert describe_args({"title": "Quiz", "questions": [{}, {}, {}]}) == 'title="Quiz", 3 questions'
    assert describe_args({"query": "x" * 200}) == f'query="{"x" * 77}..."'


def test_tool_results_are_summarized():
    profile = {
        "ok": True,
        "topics": [{"name": "Paxos", "documents": 1, "pages": 4}],
        "past_exams": {"files": 2, "average_per_exam": {"multiple_choice": 7.0, "short_answer": 2.5, "long_answer": 0.0}},
    }
    assert describe_result("get_exam_profile", profile) == [
        "1 topic: Paxos (4p)",
        "past exams: 2 files, on average 7 multiple choice, 2.5 short answer",
    ]
    assert describe_result("get_exam_profile", {"ok": True, "topics": [], "past_exams": {"files": 0}})[1] == (
        "past exams: none"
    )

    hits = [{"filename": "A.pdf", "page": 3}, {"filename": "B.pdf", "page": 1}, {"filename": "A.pdf", "page": 5}]
    assert describe_result("search_course_materials", {"ok": True, "results": hits}) == [
        "3 passages: A.pdf p.3, 5; B.pdf p.1"
    ]
    assert describe_result("search_course_materials", {"ok": True, "results": []}) == ["no passages found"]

    saved = {"ok": True, "exam_id": "x", "question_count": 1, "total_points": 2}
    assert describe_result("save_exam", saved) == ["saved: 1 question, 2 points"]
    assert describe_result("save_exam", {"ok": False, "errors": ["q1: a", "q2: b"]}) == [
        "rejected, 2 problems:",
        "q1: a",
        "q2: b",
    ]
    assert describe_result("get_exam_profile", {"ok": False, "error": "Session not found"}) == [
        "error: Session not found"
    ]


def test_each_call_and_its_result_share_a_number():
    # The model may ask for two tools at once; their results then come back after both calls.
    out = io.StringIO()
    printer = ProgressPrinter(out)
    first = ToolStep("search_course_materials", {"query": "a"})
    second = ToolStep("search_course_materials", {"query": "b"})
    printer(first)
    printer(second)
    first.result = {"ok": True, "results": []}
    printer(first)
    second.result = {"ok": False, "error": "query is empty"}
    printer(second)

    assert out.getvalue().splitlines() == [
        '[1] search_course_materials(query="a")',
        '[2] search_course_materials(query="b")',
        "[1] -> no passages found",
        "[2] -> error: query is empty",
    ]


# --- developer view ---------------------------------------------------------------------------


def test_developer_view_shows_answers_rubric_and_the_cited_pages():
    view = ExamView(
        title="Quiz",
        request="2 questions",
        questions=[
            {
                "id": "q1",
                "type": "multiple_choice",
                "topic": "View Service",
                "points": 2,
                "prompt": "Who picks the primary?",
                "choices": ["The client", "The view service"],
                "answer": "The view service",
                "rubric": [],
                "sources": [{"document_id": DOC, "page": 2}],
            },
            {
                "id": "q2",
                "type": "short_answer",
                "topic": "Split Brain",
                "points": 4,
                "prompt": "What is split brain?",
                "choices": [],
                "answer": "Two primaries at once.",
                "rubric": ["Names two primaries"],
                "sources": [{"document_id": DOC, "page": 1}, {"document_id": DOC, "page": 9}],
            },
        ],
        filenames={DOC: "Lecture 5.pdf"},
        pages={(DOC, 1): unreadable_font("Split brain happens"), (DOC, 2): "The view service\n decides " + "word " * 60},
    )
    lines = format_exam(view)

    assert lines[:5] == [
        "=== Quiz: 2 questions, 6 points ===",
        "asked for: 2 questions",
        "shape:     1 multiple choice, 1 short answer",
        "topics:    View Service 1, Split Brain 1",
        "correct multiple-choice letters, in order: b",
    ]
    text = "\n".join(lines)
    assert "q1  multiple choice | View Service | 2 points\n    Who picks the primary?" in text
    assert "    [ ] a) The client\n    [x] b) The view service" in text
    assert "    model answer: Two primaries at once.\n    rubric:\n      - Names two primaries" in text
    flat = " ".join(text.split())
    assert 'source: Lecture 5.pdf p.2: "The view service decides word word' in flat  # start of the page
    assert '..."' in flat and "word " * 60 not in flat  # clipped
    assert "source: Lecture 5.pdf p.1: (scrambled text" in flat
    assert "source: Lecture 5.pdf p.9: (page not found)" in flat


def test_long_prose_is_wrapped_but_code_keeps_its_indentation():
    lines = block("word " * 30 + "\n    int x = 1;", "  ")
    assert len(lines) == 3
    assert all(len(line) <= 100 for line in lines)
    assert lines[-1] == "      int x = 1;"
