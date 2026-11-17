"""What the model is told about each ExamWriter tool (ADK builds this from the function's
signature and docstring). No services or API calls needed."""

from google.adk.tools import FunctionTool

from app.agents.tools import AgentServices, make_exam_writer_tools


def declarations() -> dict:
    tools = make_exam_writer_tools(AgentServices(sessionmaker=None, vectors=None, embedder=None))
    result = {}
    for tool in tools:
        declaration = FunctionTool(tool)._get_declaration()
        schema = _parameter_schema(declaration)
        result[declaration.name] = {
            "params": set(schema.get("properties") or {}),
            "required": set(schema.get("required") or []),
            "description": declaration.description or "",
        }
    return result


def _parameter_schema(declaration) -> dict:
    """ADK 2.x describes parameters as plain JSON Schema (`parameters_json_schema`); older
    versions used Gemini's own Schema type (`parameters`). Read whichever is set."""
    if declaration.parameters_json_schema:
        return declaration.parameters_json_schema
    if declaration.parameters:
        return declaration.parameters.model_dump(exclude_none=True)
    return {}  # a tool without parameters


def test_exam_writer_has_exactly_three_tools():
    assert set(declarations()) == {"get_exam_profile", "search_course_materials", "save_exam"}


def test_the_model_never_sees_tool_context_or_any_id():
    for name, declaration in declarations().items():
        assert "tool_context" not in declaration["params"], name
        assert not {"user_id", "course_id", "session_id"} & declaration["params"], name


def test_parameters_the_model_can_choose():
    found = declarations()
    assert found["get_exam_profile"]["params"] == set()
    assert found["search_course_materials"]["params"] == {"query", "kind", "topic"}
    assert found["search_course_materials"]["required"] == {"query"}
    assert found["save_exam"]["params"] == {"title", "questions"}
    assert found["save_exam"]["required"] == {"title", "questions"}


def test_descriptions_explain_the_tools():
    found = declarations()
    assert "Search this study session" in found["search_course_materials"]["description"]
    assert "copied exactly from choices" in found["save_exam"]["description"]
