"""Agent tools: plain async functions that ADK lets the model call.

Each takes `tool_context` (filled in by ADK, never by the model) to read who is asking.
"""

from app.agents.tools.context import AgentServices
from app.agents.tools.exams import make_exam_tools
from app.agents.tools.materials import make_material_tools


def make_exam_writer_tools(services: AgentServices) -> list:
    """ExamWriter's tools: get_exam_profile, search_course_materials, save_exam."""
    return [*make_material_tools(services), *make_exam_tools(services)]
