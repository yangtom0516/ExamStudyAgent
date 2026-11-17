"""The exam format's shape rules (no database): services/exams.py, ExamDraft."""

import uuid

import pytest
from pydantic import ValidationError

from app.services.exams import MAX_QUESTIONS, ExamDraft, describe_errors

SOURCE = {"document_id": str(uuid.uuid4()), "page": 3}


def mc(**overrides) -> dict:
    question = {
        "type": "multiple_choice",
        "topic": "Split Brain",
        "points": 2,
        "prompt": "What prevents split brain?",
        "choices": ["Client retries", "The backup confirms the current view", "Synchronized clocks"],
        "answer": "The backup confirms the current view",
        "sources": [SOURCE],
    }
    return question | overrides


def written(**overrides) -> dict:
    question = {
        "type": "short_answer",
        "topic": "View Service",
        "points": 5,
        "prompt": "Why must the backup know if the view changed?",
        "answer": "Because a view change means the primary was declared dead...",
        "rubric": ["Mentions that the view service declared the primary dead"],
        "sources": [SOURCE],
    }
    return question | overrides


def exam(*questions) -> dict:
    return {"title": "Practice midterm", "questions": list(questions)}


def errors_of(data: dict) -> list[str]:
    with pytest.raises(ValidationError) as caught:
        ExamDraft.model_validate(data)
    return describe_errors(caught.value)


def test_valid_exam_passes():
    draft = ExamDraft.model_validate(exam(mc(), written(), written(type="long_answer")))
    assert [q.type for q in draft.questions] == ["multiple_choice", "short_answer", "long_answer"]


def test_mc_answer_takes_the_choices_spelling_and_drops_any_rubric():
    draft = ExamDraft.model_validate(exam(mc(answer="  the BACKUP confirms the current view ", rubric=["x"])))
    question = draft.questions[0]
    assert question.answer == "The backup confirms the current view"
    assert question.rubric == []


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        (mc(choices=["Only one"], answer="Only one"), "q1: multiple choice needs 2 to 6 choices"),
        (mc(choices=["A", "a", "B"], answer="A"), "q1: choices must all be different"),
        (mc(answer="Something else"), "q1: answer must be exactly one of the choices (copy the choice's text)"),
        (written(choices=["A", "B"]), "q1: short_answer questions have no choices"),
        (written(rubric=[]), "q1: written questions need at least one rubric line"),
    ],
)
def test_rules_per_question_type(question, expected):
    assert errors_of(exam(question)) == [expected]


def test_errors_name_the_question_and_field():
    errors = errors_of(exam(mc(), written(points=0, sources=[])))
    assert errors == [
        "q2.points: Input should be greater than or equal to 1",
        "q2.sources: List should have at least 1 item after validation, not 0",
    ]


def test_points_have_an_upper_limit():
    assert errors_of(exam(mc(points=101))) == ["q1.points: Input should be less than or equal to 100"]


def test_unknown_question_type_is_rejected():
    [error] = errors_of(exam(mc(type="essay")))
    assert error.startswith("q1.type: Input should be 'multiple_choice', 'short_answer' or 'long_answer'")


def test_exam_needs_a_title_and_1_to_30_questions():
    assert errors_of({"title": "  ", "questions": [mc()]}) == ["title: String should have at least 1 character"]
    assert errors_of(exam()) == ["questions: List should have at least 1 item after validation, not 0"]
    [too_many] = errors_of(exam(*[mc()] * (MAX_QUESTIONS + 1)))
    assert too_many.startswith("questions: List should have at most 30 items")


def test_source_needs_a_real_id_and_a_page_from_1():
    errors = errors_of(exam(mc(sources=[{"document_id": "lecture-5", "page": 0}])))
    assert errors[0].startswith("q1.sources.0.document_id: Input should be a valid UUID")
    assert errors[1] == "q1.sources.0.page: Input should be greater than or equal to 1"
