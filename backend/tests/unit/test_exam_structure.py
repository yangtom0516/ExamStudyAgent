from app.ingest.parsers import ParsedPage
from app.ingest.past_exams import MAX_QUESTIONS, QuestionItem, build_prompt, clean_questions
from app.services.exam_profile import summarize_past_exams


def q(number, kind, points=None):
    return QuestionItem(number=number, type=kind, points=points)


def test_types_are_normalized_and_unknown_ones_become_other():
    cleaned = clean_questions(
        [q("1", "Multiple Choice"), q("2", "short-answer"), q("3", "LONG_ANSWER"), q("4", "essay"), q("5", "")]
    )
    assert [c.type for c in cleaned] == ["multiple_choice", "short_answer", "long_answer", "other", "other"]


def test_implausible_points_are_dropped():
    cleaned = clean_questions([q("1", "other", 5), q("2", "other", 0), q("3", "other", -2), q("4", "other", 99999)])
    assert [c.points for c in cleaned] == [5, None, None, None]


def test_labels_are_trimmed_and_missing_ones_numbered():
    cleaned = clean_questions([q("  Q 2 ", "other"), q("", "other"), q("x" * 50, "other")])
    assert [c.number for c in cleaned] == ["Q 2", "2", "x" * 20]


def test_question_list_is_capped():
    assert len(clean_questions([q(str(i), "other") for i in range(MAX_QUESTIONS + 10)])) == MAX_QUESTIONS


def test_prompt_contains_title_and_pages():
    prompt = build_prompt([ParsedPage(1, "1. Which of {a, b} ..."), ParsedPage(2, "")], title="Midterm 2025")
    assert '"Midterm 2025"' in prompt
    assert "=== Page 1 ===\n1. Which of {a, b} ..." in prompt
    assert "=== Page 2 ===\n(no text)" in prompt


def test_summary_counts_averages_and_points():
    # Two exams: 20 MC worth 40 points, 6 short (only some with points), 3 long.
    summary = summarize_past_exams(
        files=2,
        rows=[("multiple_choice", 20, 40, 20), ("short_answer", 6, 20, 4), ("long_answer", 3, 45, 3)],
    )
    assert summary.questions == {"multiple_choice": 20, "short_answer": 6, "long_answer": 3, "other": 0}
    assert summary.average_per_exam == {"multiple_choice": 10.0, "short_answer": 3.0, "long_answer": 1.5, "other": 0.0}
    assert summary.points == {"multiple_choice": 40, "short_answer": 20, "long_answer": 45, "other": 0}
    assert summary.average_points_per_exam == 52.5


def test_summary_without_past_exams_is_all_zeros():
    summary = summarize_past_exams(files=0, rows=[])
    assert summary.files == 0
    assert set(summary.questions.values()) == {0}
    assert set(summary.average_per_exam.values()) == {0.0}
    assert summary.average_points_per_exam is None


def test_summary_without_printed_points_has_no_average_points():
    summary = summarize_past_exams(files=1, rows=[("multiple_choice", 10, None, 0)])
    assert summary.points["multiple_choice"] == 0
    assert summary.average_points_per_exam is None
