You are indexing course material for a study app. Below is the text of one document, "{title}", page by page.

List the main topics this document teaches, with the pages where each one is covered.

Rules:
- About 3 to 15 topics for a typical lecture; fewer for a short document.
- A topic is a specific concept a student could be examined on, such as "Binary Heaps", "Dijkstra's Algorithm" or "Amortized Analysis". Not too broad ("Data Structures", "Introduction") and not a one-slide detail.
- A topic usually spans a few pages, up to about 15. If one concept seems to run much longer, split that stretch into the separate ideas it teaches.
- Every page that teaches something belongs to at least one topic. Skip only administrative pages: title page, outline, syllabus, logistics, references.
- Give each topic a page range (page_start to page_end, inclusive). If a topic comes back later in the document, list it again with the new range.
- Use short names in Title Case.
- The topic names already used in this course are listed below, for reference only. Reuse one, spelled exactly the same, only when this document teaches that same concept, so one concept never gets two names (such as "Heaps" and "Binary Heaps"). Everything else gets a new name of its own, even when it is related to an existing topic: a lecture on Dijkstra's Algorithm in a course that already has "Breadth-First Search" gets the topic "Dijkstra's Algorithm", not "Breadth-First Search".

Topic names already used in this course (reference only):
{existing_topics}

Document pages:
{pages}
