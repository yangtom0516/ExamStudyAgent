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


def test_sub_parts_of_a_written_question_count_once():
    # The EECS 491 midterm as Gemini listed it: points printed once per question, after its last part.
    cleaned = clean_questions(
        [
            q("1a", "long_answer"),
            q("1b", "long_answer", 25),
            q("2", "short_answer", 15),
            q("3a", "long_answer"),
            q("3b", "long_answer"),
            q("3c", "long_answer", 30),
            q("4a", "long_answer"),
            q("4b", "long_answer"),
            q("4c", "long_answer", 20),
            q("5a", "short_answer"),
            q("5b", "short_answer", 10),
        ]
    )
    assert [(c.number, c.type, c.points) for c in cleaned] == [
        ("1", "long_answer", 25),
        ("2", "short_answer", 15),
        ("3", "long_answer", 30),
        ("4", "long_answer", 20),
        ("5", "short_answer", 10),
    ]


def test_sub_part_labels_in_other_styles_are_merged_too():
    cleaned = clean_questions(
        [
            q("Q4(a)", "short_answer", 3),
            q("Q4 (b)", "short_answer", 4),
            q("2.a", "long_answer"),
            q("2.b", "long_answer"),
            q("6(i)", "long_answer"),
            q("6(ii)", "long_answer"),
        ]
    )
    assert [(c.number, c.points) for c in cleaned] == [("Q4", 7), ("2", None), ("6", None)]


def test_sub_parts_of_different_types_stay_separate():
    cleaned = clean_questions([q("3a", "short_answer", 5), q("3b", "long_answer", 10)])
    assert [c.number for c in cleaned] == ["3a", "3b"]


def test_multiple_choice_items_are_never_merged():
    # "1a" to "1c" in a multiple-choice section are three questions.
    cleaned = clean_questions([q("1a", "multiple_choice", 2), q("1b", "multiple_choice", 2), q("1c", "multiple_choice")])
    assert len(cleaned) == 3


def test_separate_questions_are_not_merged():
    # "3.1"/"3.2" are often questions 1 and 2 of section 3; 4a and 5a are different questions.
    cleaned = clean_questions(
        [q("3.1", "short_answer"), q("3.2", "short_answer"), q("4a", "long_answer"), q("5a", "long_answer")]
    )
    assert [c.number for c in cleaned] == ["3.1", "3.2", "4a", "5a"]


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
