from app.ingest.parsers import ParsedPage
from app.ingest.topics import (
    MAX_TOPICS,
    TopicRange,
    build_prompt,
    clean_topics,
    normalize_topic_name,
    topics_by_page,
)


def topic(name, start, end):
    return TopicRange(name=name, page_start=start, page_end=end)


def test_normalized_names_match_despite_case_spacing_and_punctuation():
    assert normalize_topic_name("Binary Heaps") == "binary heaps"
    assert normalize_topic_name("  binary   heaps. ") == "binary heaps"
    assert normalize_topic_name("Dijkstra's Algorithm") == "dijkstra s algorithm"
    assert normalize_topic_name("C++ Templates") == "c++ templates"
    assert normalize_topic_name("C#") != normalize_topic_name("C")


def test_clean_topics_clamps_pages_and_fixes_reversed_ranges():
    cleaned = clean_topics([topic("Heaps", 0, 50), topic("Graphs", 9, 7)], page_count=10)
    assert [(t.page_start, t.page_end) for t in cleaned] == [(1, 10), (7, 9)]


def test_clean_topics_drops_junk_names_and_duplicates():
    cleaned = clean_topics(
        [
            topic("  ", 1, 2),  # empty
            topic("...", 1, 2),  # only punctuation
            topic("x" * 150, 1, 2),  # too long
            topic("Heaps", 1, 2),
            topic("heaps", 1, 3),  # same topic, same start page: duplicate
            topic("Heaps", 8, 9),  # same topic later in the document: kept
        ],
        page_count=10,
    )
    assert [(t.name, t.page_start) for t in cleaned] == [("Heaps", 1), ("Heaps", 8)]


def test_clean_topics_keeps_at_most_max_distinct_topics():
    many = [topic(f"Topic {i}", 1, 1) for i in range(MAX_TOPICS + 5)]
    assert len(clean_topics(many, page_count=1)) == MAX_TOPICS


def test_topics_by_page_tags_every_page_in_a_range():
    pages = topics_by_page([topic("Heaps", 2, 3), topic("Priority Queues", 3, 4)])
    assert pages == {2: ["heaps"], 3: ["heaps", "priority queues"], 4: ["priority queues"]}


def test_prompt_contains_pages_existing_topics_and_title():
    prompt = build_prompt(
        [ParsedPage(1, "Heaps intro {not a placeholder}"), ParsedPage(2, "")],
        existing=["Graphs", "Heaps"],
        title="Lecture 5",
    )
    assert '"Lecture 5"' in prompt
    assert "- Graphs\n- Heaps" in prompt
    assert "=== Page 1 ===\nHeaps intro {not a placeholder}" in prompt
    assert "=== Page 2 ===\n(no text)" in prompt


def test_prompt_says_when_the_course_has_no_topics_yet():
    assert "(none yet)" in build_prompt([ParsedPage(1, "x")], existing=[], title="t")
