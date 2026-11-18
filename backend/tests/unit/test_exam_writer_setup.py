"""How the ExamWriter agent is put together (no services or API calls)."""

from pathlib import Path

from app.agents.exam_writer import build_exam_writer, on_tool_error
from app.agents.tools import AgentServices
from app.config import get_settings
from tests.helpers import ScriptedModel

PROMPT_FILE = Path(__file__).resolve().parents[2] / "app" / "agents" / "prompts" / "exam_writer.md"


def agent(model=None):
    return build_exam_writer(AgentServices(None, None, None), get_settings(), model or ScriptedModel())


def test_exam_writer_has_its_three_tools_and_safety_net():
    exam_writer = agent()
    assert exam_writer.name == "exam_writer"
    assert [tool.__name__ for tool in exam_writer.tools] == ["get_exam_profile", "search_course_materials", "save_exam"]
    assert exam_writer.on_tool_error_callback is on_tool_error
    assert "Writes a mock exam" in exam_writer.description  # what StudyCoach will read (step 6)


def test_instructions_come_from_the_prompt_file_unchanged():
    # Given as a function, so ADK doesn't treat the {...} in the prompt as template variables.
    assert agent().instruction(None) == PROMPT_FILE.read_text(encoding="utf-8")


def test_prompt_keeps_the_key_rules():
    prompt = PROMPT_FILE.read_text(encoding="utf-8")
    assert "Never invent a source" in prompt
    assert "Every fact in a model answer or rubric line must be stated in the cited passages" in prompt
    assert "about one search per question" in prompt  # each search is re-sent on every later call
    assert "several search_course_materials calls at once" in prompt
    assert "Never include questions, answers or the rubric in the reply" in prompt
    assert "technical error twice" in prompt


def test_scripted_model_replaces_the_configured_one():
    model = ScriptedModel()
    assert agent(model).model is model
