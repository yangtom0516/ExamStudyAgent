"""ExamWriter: an LLM agent that writes a mock exam from the session's material and saves it.

The agent decides itself which tools to call and in what order (prompts/exam_writer.md says what
a good exam is, not a fixed sequence). ADK runs the loop: send the conversation to the model, run
the tool it asks for, append the result, repeat until the model answers in plain text.

`run_exam_writer` runs it once on its own (the terminal script in 4d, evals). In step 6 the agent
becomes a tool of StudyCoach instead.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from google.adk.agents import LlmAgent, RunConfig
from google.adk.agents.invocation_context import LlmCallsLimitExceededError
from google.adk.models.base_llm import BaseLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agents.models import make_model
from app.agents.tools import AgentServices, make_exam_writer_tools
from app.agents.tools.context import LAST_EXAM_ID, Identity, initial_state
from app.config import Settings

logger = logging.getLogger(__name__)

APP_NAME = "exam_study_agent"
PROMPT = (Path(__file__).parent / "prompts" / "exam_writer.md").read_text(encoding="utf-8")
# Read by StudyCoach (step 6) to decide when to call ExamWriter.
DESCRIPTION = (
    "Writes a mock exam from this study session's lecture slides and notes, checks it and saves "
    "it. Give it the student's request: topics, number and types of questions."
)

TECHNICAL_ERROR = (
    "{tool} failed for a technical reason (not your mistake). Try once more; if it fails again, "
    "stop and tell the student to try again later."
)
CALL_LIMIT_MESSAGE = "I couldn't finish that; try a smaller request."
FAILED_MESSAGE = "Something went wrong while writing the exam; please try again."
SAVED_ANYWAY = " The exam was saved before that happened, so you can still open it."


async def on_tool_error(tool, args: dict[str, Any], tool_context, error: Exception) -> dict:
    """Safety net: a tool that crashes (database or Qdrant down, Gemini embedding failing) gives the
    model an error it can react to, instead of aborting the whole run. Logged in full for us."""
    logger.error("Tool %s failed with args %s", tool.name, args, exc_info=error)
    return {"ok": False, "error": TECHNICAL_ERROR.format(tool=tool.name)}


def _instruction(_context) -> str:
    # Given as a function so ADK uses the text as-is: a plain string would have its {...} parts
    # treated as template variables, and the prompt contains JSON-like examples.
    return PROMPT


def build_exam_writer(services: AgentServices, settings: Settings, model: BaseLlm | None = None) -> LlmAgent:
    """`model` replaces the configured one (tests use a scripted fake)."""
    return LlmAgent(
        name="exam_writer",
        description=DESCRIPTION,
        model=model or make_model(settings.exam_model, settings),
        instruction=_instruction,
        tools=make_exam_writer_tools(services),
        on_tool_error_callback=on_tool_error,
    )


# --- running it once --------------------------------------------------------------------


@dataclass
class ToolStep:
    """One tool call: what the model asked for and, once it ran, what it got back."""

    name: str
    args: dict[str, Any]
    result: dict[str, Any] | None = None


@dataclass
class ExamWriterRun:
    reply: str = ""  # the agent's final text, or a friendly message if the run failed
    exam_id: str | None = None  # set whenever an exam was saved, even if the run failed afterwards
    error: str | None = None  # None, "call_limit" or "failed"
    steps: list[ToolStep] = field(default_factory=list)
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0  # includes the model's thinking tokens (billed as output)


ProgressCallback = Callable[[ToolStep], None]


async def run_exam_writer(
    services: AgentServices,
    settings: Settings,
    *,
    identity: Identity,
    request: str,
    model: BaseLlm | None = None,
    max_llm_calls: int | None = None,
    on_progress: ProgressCallback | None = None,
) -> ExamWriterRun:
    """Run ExamWriter once for `request`. Never raises: failures come back in `error`/`reply`.

    `on_progress` is called when the model asks for a tool (result still None) and again when the
    tool's result is in, so a caller can show progress live.
    """
    agent = build_exam_writer(services, settings, model)
    session_service = InMemorySessionService()  # chat history is kept in Postgres from step 6
    state = initial_state(
        user_id=identity.user_id, course_id=identity.course_id, session_id=identity.session_id, request=request
    )
    user_id = str(identity.user_id)
    session = await session_service.create_session(app_name=APP_NAME, user_id=user_id, state=state)
    runner = Runner(app_name=APP_NAME, agent=agent, session_service=session_service)
    config = RunConfig(max_llm_calls=max_llm_calls or settings.exam_max_llm_calls)
    message = types.Content(role="user", parts=[types.Part(text=request)])

    run = ExamWriterRun()
    pending: dict[str, ToolStep] = {}
    try:
        async for event in runner.run_async(
            user_id=user_id, session_id=session.id, new_message=message, run_config=config
        ):
            _record(event, run, pending, on_progress)
    except LlmCallsLimitExceededError:
        logger.warning("ExamWriter hit the limit of %d model calls", config.max_llm_calls)
        run.error = "call_limit"
    except Exception:
        logger.exception("ExamWriter run failed")
        run.error = "failed"
    finally:
        await runner.close()

    finished = await session_service.get_session(app_name=APP_NAME, user_id=user_id, session_id=session.id)
    run.exam_id = (finished.state.get(LAST_EXAM_ID) if finished else None) or None
    if run.error:
        run.reply = (CALL_LIMIT_MESSAGE if run.error == "call_limit" else FAILED_MESSAGE) + (
            SAVED_ANYWAY if run.exam_id else ""
        )
    return run


def _record(event, run: ExamWriterRun, pending: dict[str, ToolStep], on_progress: ProgressCallback | None) -> None:
    """Collect tool calls, results, the final text and token usage from one ADK event."""
    if event.usage_metadata and not event.partial:  # one complete model response = one model call
        usage = event.usage_metadata
        run.llm_calls += 1
        run.input_tokens += usage.prompt_token_count or 0
        run.output_tokens += (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)

    if not event.content or not event.content.parts:
        return
    texts = []
    for part in event.content.parts:
        if part.function_call:
            step = ToolStep(name=part.function_call.name, args=dict(part.function_call.args or {}))
            run.steps.append(step)
            pending[part.function_call.id or step.name] = step
            if on_progress:
                on_progress(step)
        elif part.function_response:
            key = part.function_response.id or part.function_response.name
            step = pending.pop(key, None)
            if step is not None:
                step.result = dict(part.function_response.response or {})
                if on_progress:
                    on_progress(step)
        elif part.text and not part.thought:
            texts.append(part.text)
    if texts and event.is_final_response():
        run.reply = "".join(texts).strip()
