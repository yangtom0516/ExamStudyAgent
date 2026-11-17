"""Tools for reading the session's material: hybrid search and the exam profile.

The docstrings of the inner functions are what the model reads as each tool's description.
"""

from google.adk.tools import ToolContext

from app.agents.tools.context import AgentServices, MissingIdentityError, identity_from, session_is_theirs, tool_error
from app.services.exam_profile import build_exam_profile
from app.services.search import search

MAX_RESULTS = 5  # passages per search; every passage stays in the conversation, so this caps cost
MAX_PASSAGE_CHARS = 1200  # slides can be long (up to 4000 chars); the start carries the point
MAX_QUERY_CHARS = 500
KINDS = ("lecture", "notes")


def clip(text: str, limit: int = MAX_PASSAGE_CHARS) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + " …"


def make_material_tools(services: AgentServices) -> list:
    async def search_course_materials(
        query: str, tool_context: ToolContext, kind: str | None = None, topic: str | None = None
    ) -> dict:
        """Search this study session's lecture slides and notes. Use it before writing questions
        on a topic: every question must be based on passages found here.

        Args:
            query: What to look for, in words or as a question, e.g. "how the backup confirms the
                current view". Exact terms like "Theorem 3.2" or "Paxos" are matched too.
            kind: Optional. "lecture" or "notes" to search only that kind of file.
            topic: Optional. A topic name from get_exam_profile, to search only passages tagged
                with that topic.

        Returns:
            Up to 5 passages, best first. Each has document_id, filename, page, topics and text.
            Cite a passage in a question's sources as {"document_id": ..., "page": ...}.
            An empty list means this session has no material matching the search.
        """
        try:
            who = identity_from(tool_context)
        except MissingIdentityError as exc:
            return tool_error(str(exc))
        query = query.strip()[:MAX_QUERY_CHARS]
        if not query:
            return tool_error("query is empty")
        kind = kind or None  # models sometimes send "" for an argument they meant to leave out
        if kind is not None and kind not in KINDS:
            return tool_error('kind must be "lecture" or "notes", or left out')

        hits = await search(
            services.vectors,
            services.embedder,
            user_id=who.user_id,
            session_id=who.session_id,
            query=query,
            kind=kind,
            topic=topic or None,
            limit=MAX_RESULTS,
        )
        results = [
            {
                "document_id": hit.document_id,
                "filename": hit.filename,
                "page": hit.page_number,
                "topics": hit.topics,
                "text": clip(hit.text),
            }
            for hit in hits
        ]
        if not results:
            return {
                "ok": True,
                "results": [],
                "note": "No material found. Try other words or no topic filter; if there is still "
                "nothing, write fewer questions on this and say why.",
            }
        return {"ok": True, "results": results}

    async def get_exam_profile(tool_context: ToolContext) -> dict:
        """What this study session covers and what its past exams looked like. Call it first.

        Returns:
            topics: every topic in this session's lectures/notes with how many files and pages
                cover it (more pages = more material to write questions from). A question's topic
                must be one of these names.
            past_exams: the structure of the uploaded past exams: number of files, questions per
                type in total and on average per exam, and printed points. Unless the student asks
                for something else, give the exam a similar shape. All zeros if none were uploaded.
        """
        try:
            who = identity_from(tool_context)
        except MissingIdentityError as exc:
            return tool_error(str(exc))
        async with services.sessionmaker() as db:
            if not await session_is_theirs(db, who):
                return tool_error("Session not found")
            profile = await build_exam_profile(db, who.session_id)
        return {"ok": True, **profile.model_dump()}

    return [get_exam_profile, search_course_materials]
