"""The tool that hands a finished exam to the checker in services/exams.py and saves it.

Every rule is enforced there; on any problem nothing is saved and the problems come back as
sentences naming the question, so the model can fix them and call save_exam again.
"""

from google.adk.tools import ToolContext

from app.agents.tools.context import LAST_EXAM_ID, REQUEST, AgentServices, MissingIdentityError, identity_from, tool_error
from app.services import exams


def make_exam_tools(services: AgentServices) -> list:
    async def save_exam(title: str, questions: list[dict], tool_context: ToolContext) -> dict:
        """Check the finished exam and save it. If it returns errors, fix exactly those and call
        save_exam again with the whole corrected exam.

        Args:
            title: A short exam title, e.g. "Primary/Backup Practice Quiz".
            questions: 1 to 30 questions, in order. Each question is an object with:
                type: "multiple_choice", "short_answer" or "long_answer".
                topic: one of the topic names from get_exam_profile.
                points: whole number from 1 to 100.
                prompt: the question text.
                choices: multiple_choice only: 2 to 6 different answer options. Leave out for
                    written questions.
                answer: multiple_choice: the correct option, copied exactly from choices.
                    Written questions: a model answer.
                rubric: written questions only: 1 to 10 grading points, each one thing a good
                    answer must contain, e.g. "Explains that the backup must confirm the view".
                sources: 1 to 10 pages the question is based on, each {"document_id": ...,
                    "page": ...}, copied from search_course_materials results.

        Returns:
            On success: ok=true, exam_id, question_count and total_points.
            Otherwise: ok=false and errors, e.g. "q3: page 57 doesn't exist in Lecture 5.pdf".
        """
        try:
            who = identity_from(tool_context)
        except MissingIdentityError as exc:
            return tool_error(str(exc))

        draft = {"title": title, "questions": questions}
        async with services.sessionmaker() as db:
            result = await exams.save_exam(
                db,
                user_id=who.user_id,
                session_id=who.session_id,
                exam=draft,
                request=str(tool_context.state.get(REQUEST, "")),
            )
        if not result.ok:
            return {"ok": False, "errors": result.errors}

        tool_context.state[LAST_EXAM_ID] = str(result.exam_id)
        return {
            "ok": True,
            "exam_id": str(result.exam_id),
            "question_count": len(questions),
            "total_points": _total_points(questions),
        }

    return [save_exam]


def _total_points(questions: list[dict]) -> int:
    # Only reached after the checker accepted every question, so points are valid integers.
    return sum(int(q["points"]) for q in questions)
