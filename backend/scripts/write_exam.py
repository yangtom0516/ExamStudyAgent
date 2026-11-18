"""Try ExamWriter on a real study session with real Gemini. Each run costs money (roughly $0.05-0.30).

Run from backend/ with the data stores up (docker compose up -d):

    uv run python scripts/write_exam.py                            list sessions to pick from
    uv run python scripts/write_exam.py <session id> "<request>"   write an exam
        --model gemini-3.5-flash-lite    use another model for this run
        --max-calls 15                   change the cap on model calls (default: EXAM_MAX_LLM_CALLS)

It prints every tool call as it happens, the agent's reply (what a student would see), the calls,
tokens and cost of the run, and then the saved exam WITH answers, rubric and the start of each
cited page: a developer view no student gets, for checking that every question is right and really
comes from the slides it cites.

A developer tool: it reads the database directly and runs as the session's owner.
"""

import argparse
import asyncio
import json
import logging
import sys
import textwrap
import time
import uuid
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TextIO

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # so `app` can be imported when run as a file

from google.adk.models.base_llm import BaseLlm  # noqa: E402
from qdrant_client import AsyncQdrantClient  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.agents.exam_writer import ExamWriterRun, ToolStep, run_exam_writer  # noqa: E402
from app.agents.models import make_model  # noqa: E402
from app.agents.tools import AgentServices  # noqa: E402
from app.agents.tools.context import Identity  # noqa: E402
from app.config import Settings, get_settings  # noqa: E402
from app.db.models import (  # noqa: E402
    Course,
    Document,
    DocumentKind,
    DocumentStatus,
    Exam,
    Page,
    SessionStatus,
    StudySession,
    User,
)
from app.ingest.embedding import GeminiEmbedder  # noqa: E402
from app.ingest.parsers import looks_garbled  # noqa: E402
from app.services.vectors import VectorStore  # noqa: E402

SCRIPT = "uv run python scripts/write_exam.py"
WIDTH = 100  # wrap long lines of prose at this many characters
SNIPPET_CHARS = 160  # how much of a cited page to show
MATERIAL_KINDS = (DocumentKind.LECTURE, DocumentKind.NOTES)  # what questions can be written from
TYPE_NAMES = {"multiple_choice": "multiple choice", "short_answer": "short answer", "long_answer": "long answer"}
LETTERS = "abcdef"

# USD per million tokens: model -> [(valid from, input, output), ...], oldest first. Thinking tokens
# are billed as output. Ignores Gemini's automatic caching discount, so it's an upper estimate.
PRICES: dict[str, list[tuple[date, float, float]]] = {
    "gemini-3.8-flash": [(date.min, 0.75, 3.75), (date(2027, 1, 1), 1.50, 7.50)],  # launch price until 2026-12-31
    "gemini-3.5-flash-lite": [(date.min, 0.30, 2.50)],
}


class ScriptError(Exception):
    """A problem the user can fix; printed as a message, without a traceback."""


# --- small text helpers ---------------------------------------------------------------------


def plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def fit(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 3].rstrip() + "..."


def block(text: str, first: str, rest: str | None = None) -> list[str]:
    """`text` indented by `first` (later lines by `rest`), long prose lines wrapped. Lines that
    start with whitespace (code in a question) are kept as they are."""
    rest = first if rest is None else rest
    lines: list[str] = []
    for line in text.splitlines() or [""]:
        prefix = rest if lines else first
        if line[:1].isspace() or len(prefix) + len(line) <= WIDTH:
            lines.append(prefix + line)
        else:
            lines += textwrap.wrap(line, WIDTH, initial_indent=prefix, subsequent_indent=rest)
    return lines


# --- picking a session ----------------------------------------------------------------------


@dataclass
class SessionRow:
    id: uuid.UUID
    username: str
    course: str
    name: str
    status: str
    materials: int = 0  # ready lecture and notes files
    past_exams: int = 0  # ready past exams
    not_ready: int = 0
    scrambled: Counter = field(default_factory=Counter)  # filename -> pages stored as scrambled text

    @property
    def warnings(self) -> str:
        notes = [f"{plural(n, 'scrambled page')} in {name}" for name, n in self.scrambled.most_common()]
        if self.not_ready:
            notes.append(f"{plural(self.not_ready, 'file')} not ready")
        if self.status == SessionStatus.ACTIVE and not self.materials:
            notes.append("no lectures or notes to write from")
        return "; ".join(notes)


async def list_sessions(db: AsyncSession) -> list[SessionRow]:
    """Every study session with what it holds, and pages whose text is still scrambled (stored
    before the step 3 font fix): the agent would search gibberish there."""
    rows = {
        session_id: SessionRow(session_id, username, course, name, status)
        for session_id, username, course, name, status in await db.execute(
            select(StudySession.id, User.username, Course.name, StudySession.name, StudySession.status)
            .join(Course, StudySession.course_id == Course.id)
            .join(User, Course.user_id == User.id)
            .order_by(User.username, Course.name, StudySession.created_at)
        )
    }
    for session_id, kind, status in await db.execute(select(Document.session_id, Document.kind, Document.status)):
        row = rows[session_id]
        if status != DocumentStatus.READY:
            row.not_ready += 1
        elif kind == DocumentKind.PAST_EXAM:
            row.past_exams += 1
        else:
            row.materials += 1
    for session_id, filename, text in await db.execute(
        select(Document.session_id, Document.filename, Page.text)
        .join(Page, Page.document_id == Document.id)
        .where(Document.status == DocumentStatus.READY, Document.kind.in_(MATERIAL_KINDS))
    ):
        if looks_garbled(text):
            rows[session_id].scrambled[filename] += 1
    return list(rows.values())


def format_sessions(rows: list[SessionRow]) -> list[str]:
    if not rows:
        return ["No study sessions yet. Create one (http://127.0.0.1:8000/docs), upload files and start it."]
    lines = [
        f"{'session id':36}  {'user':12}  {'course':16}  {'session':22}  {'status':6}  "
        f"{'lectures':>8}  {'past exams':>10}  warnings"
    ]
    for row in rows:
        lines.append(
            f"{row.id!s:36}  {fit(row.username, 12):12}  {fit(row.course, 16):16}  {fit(row.name, 22):22}  "
            f"{row.status:6}  {row.materials:>8}  {row.past_exams:>10}  {row.warnings}"
        )
    lines += ["", f'Write an exam: {SCRIPT} <session id> "<request>"   (the session must be active)']
    return lines


@dataclass(frozen=True)
class Target:
    identity: Identity
    course: str
    session: str


async def find_session(db: AsyncSession, session_id: str) -> Target:
    """The session to write for, checked before any money is spent."""
    try:
        sid = uuid.UUID(session_id)
    except ValueError:
        raise ScriptError(f"{session_id!r} isn't a session id. Run `{SCRIPT}` to list sessions.") from None
    found = (
        await db.execute(
            select(StudySession, Course).join(Course, StudySession.course_id == Course.id).where(StudySession.id == sid)
        )
    ).first()
    if found is None:
        raise ScriptError(f"No study session has the id {sid}. Run `{SCRIPT}` to list sessions.")
    study_session, course = found
    if study_session.status != SessionStatus.ACTIVE:
        raise ScriptError(
            f"Session {study_session.name!r} hasn't been started, and exams can only be saved in a started "
            f"session. Start it first (POST /sessions/{sid}/start)."
        )
    materials = await db.scalar(
        select(func.count())
        .select_from(Document)
        .where(Document.session_id == sid, Document.status == DocumentStatus.READY, Document.kind.in_(MATERIAL_KINDS))
    )
    if not materials:
        raise ScriptError(f"Session {study_session.name!r} has no lecture slides or notes to write questions from.")
    return Target(Identity(user_id=course.user_id, course_id=course.id, session_id=sid), course.name, study_session.name)


# --- live progress ----------------------------------------------------------------------------


def describe_args(args: dict) -> str:
    """A tool call's arguments in one short line: lists are counted ("5 questions"), long text clipped."""
    parts = []
    for key, value in args.items():
        if value is None:
            continue
        if isinstance(value, list):
            parts.append(plural(len(value), key.removesuffix("s")))
        elif isinstance(value, str):
            parts.append(f'{key}="{fit(value, 80)}"')
        else:
            parts.append(f"{key}={value!r}")
    return ", ".join(parts)


def describe_result(name: str, result: dict) -> list[str]:
    """What a tool gave the model, in a line or a few."""
    if result.get("ok") is False:
        if "errors" in result:
            errors = result["errors"]
            return [f"rejected, {plural(len(errors), 'problem')}:", *errors]
        return [f"error: {result.get('error', result)}"]

    if name == "get_exam_profile":
        topics = result.get("topics", [])
        past = result.get("past_exams", {})
        topic_list = ", ".join(f"{t['name']} ({t['pages']}p)" for t in topics)
        if past.get("files"):
            shape = ", ".join(f"{n:g} {TYPE_NAMES.get(t, t)}" for t, n in past["average_per_exam"].items() if n)
            exams = f"past exams: {plural(past['files'], 'file')}, on average {shape or 'no questions found'}"
        else:
            exams = "past exams: none"
        return [f"{plural(len(topics), 'topic')}: {topic_list}", exams]

    if name == "search_course_materials":
        hits = result.get("results", [])
        if not hits:
            return ["no passages found"]
        pages_by_file: dict[str, list[str]] = {}
        for hit in hits:
            pages_by_file.setdefault(hit["filename"], []).append(str(hit["page"]))
        where = "; ".join(f"{filename} p.{', '.join(pages)}" for filename, pages in pages_by_file.items())
        return [f"{plural(len(hits), 'passage')}: {where}"]

    if name == "save_exam" and result.get("exam_id"):
        return [f"saved: {plural(result['question_count'], 'question')}, {result['total_points']} points"]

    return [fit(json.dumps(result, default=str), 200)]


class ProgressPrinter:
    """on_progress for run_exam_writer: prints each tool call when the model asks for it, then a
    summary of its result. Both carry the step number, since the model may ask for several at once."""

    def __init__(self, out: TextIO) -> None:
        self.out = out
        self.numbers: dict[int, int] = {}

    def __call__(self, step: ToolStep) -> None:
        number = self.numbers.setdefault(id(step), len(self.numbers) + 1)
        if step.result is None:
            print(f"[{number}] {step.name}({describe_args(step.args)})", file=self.out, flush=True)
            return
        first, *more = describe_result(step.name, step.result)
        print(f"[{number}] -> {first}", file=self.out)
        for line in more:
            print(f"        {line}", file=self.out)
        self.out.flush()


# --- after the run --------------------------------------------------------------------------


def estimate_cost(model: str, input_tokens: int, output_tokens: int, today: date | None = None) -> float | None:
    """Approximate USD cost of a run, or None for a model not in PRICES."""
    periods = PRICES.get(model.removeprefix("models/"))
    if not periods:
        return None
    today = today or date.today()
    _, per_input, per_output = [p for p in periods if p[0] <= today][-1]
    return (input_tokens * per_input + output_tokens * per_output) / 1_000_000


def format_stats(run: ExamWriterRun, model: str, max_calls: int, seconds: float, today: date | None = None) -> str:
    cost = estimate_cost(model, run.input_tokens, run.output_tokens, today)
    cost_text = f"about ${cost:.3f}" if cost is not None else "cost unknown for this model"
    return (
        f"{model} | {run.llm_calls} of {max_calls} model calls | {run.input_tokens:,} input + "
        f"{run.output_tokens:,} output tokens | {cost_text} | {seconds:.0f} s"
    )


@dataclass
class ExamView:
    """A saved exam with what's needed to check its sources."""

    title: str
    request: str
    questions: list[dict]
    filenames: dict[str, str]  # document id -> filename
    pages: dict[tuple[str, int], str]  # (document id, page number) -> the page's stored text


async def load_exam(db: AsyncSession, exam_id: str) -> ExamView | None:
    exam = await db.get(Exam, uuid.UUID(str(exam_id)))
    if exam is None:
        return None
    cited = {(s["document_id"], s["page"]) for q in exam.questions for s in q["sources"]}
    document_ids = {uuid.UUID(document_id) for document_id, _ in cited}
    filenames = {
        str(document_id): filename
        for document_id, filename in await db.execute(
            select(Document.id, Document.filename).where(Document.id.in_(document_ids))
        )
    }
    pages = {}
    for document_id, number, text in await db.execute(
        select(Page.document_id, Page.page_number, Page.text).where(Page.document_id.in_(document_ids))
    ):
        if (str(document_id), number) in cited:
            pages[(str(document_id), number)] = text
    return ExamView(exam.title, exam.request, exam.questions, filenames, pages)


def describe_source(view: ExamView, source: dict) -> str:
    """'Lecture 5.pdf p.12: "start of that page..."', or why the page can't be shown."""
    filename = view.filenames.get(source["document_id"], source["document_id"])
    text = view.pages.get((source["document_id"], source["page"]))
    if text is None:
        snippet = "(page not found)"
    elif looks_garbled(text):
        snippet = "(scrambled text, stored before the step 3 font fix)"
    elif not text.strip():
        snippet = "(no text on this page)"
    else:
        snippet = f'"{fit(" ".join(text.split()), SNIPPET_CHARS)}"'
    return f"{filename} p.{source['page']}: {snippet}"


def format_exam(view: ExamView) -> list[str]:
    """The whole exam with answers, rubric and sources, plus its shape at a glance."""
    questions = view.questions
    types = Counter(TYPE_NAMES[q["type"]] for q in questions)
    topics = Counter(q["topic"] for q in questions)
    lines = [
        f"=== {view.title}: {plural(len(questions), 'question')}, {sum(q['points'] for q in questions)} points ===",
        *block(view.request, "asked for: ", "           "),
        "shape:     " + ", ".join(f"{n} {kind}" for kind, n in types.items()),
        "topics:    " + ", ".join(f"{topic} {n}" for topic, n in topics.most_common()),
    ]
    correct = [LETTERS[q["choices"].index(q["answer"])] for q in questions if q["answer"] in q.get("choices", [])]
    if correct:
        lines.append("correct multiple-choice letters, in order: " + " ".join(correct))

    for q in questions:
        lines += ["", f"{q['id']}  {TYPE_NAMES[q['type']]} | {q['topic']} | {plural(q['points'], 'point')}"]
        lines += block(q["prompt"], "    ")
        if q["type"] == "multiple_choice":
            for letter, choice in zip(LETTERS, q["choices"]):
                mark = "[x]" if choice == q["answer"] else "[ ]"
                lines += block(choice, f"    {mark} {letter}) ", " " * 11)
        else:
            lines += block(q["answer"], "    model answer: ", " " * 18)
            lines.append("    rubric:")
            for item in q["rubric"]:
                lines += block(item, "      - ", " " * 8)
        for source in q["sources"]:
            lines += block(describe_source(view, source), "    source: ", " " * 12)
    return lines


# --- running it -----------------------------------------------------------------------------


def load_model(name: str, settings: Settings) -> BaseLlm:
    """The model for this run, or a clear message before anything is spent."""
    if not settings.google_api_key.get_secret_value():
        raise ScriptError("GOOGLE_API_KEY is not set in .env (the model and the search both need it).")
    try:
        return make_model(name, settings)
    except RuntimeError as exc:
        raise ScriptError(str(exc)) from exc


async def write_exam(
    services: AgentServices,
    settings: Settings,
    *,
    session_id: str,
    request: str,
    model: BaseLlm,
    max_llm_calls: int,
    out: TextIO,
) -> ExamWriterRun:
    """Run ExamWriter once on a session and print everything about the run to `out`."""
    async with services.sessionmaker() as db:
        target = await find_session(db, session_id)
    print(f"Course {target.course!r}, session {target.session!r}", file=out)
    print(*block(request, "Request: ", "         "), sep="\n", file=out)
    print(f"Model {model.model}, at most {max_llm_calls} model calls", file=out)
    print("(The step that writes the questions can take a few minutes with no output.)\n", file=out, flush=True)

    started = time.monotonic()
    run = await run_exam_writer(
        services,
        settings,
        identity=target.identity,
        request=request,
        model=model,
        max_llm_calls=max_llm_calls,
        on_progress=ProgressPrinter(out),
    )
    seconds = time.monotonic() - started

    print("\nReply (what the student sees):", file=out)
    print(*block(run.reply or "(no reply)", "  "), sep="\n", file=out)
    print("\n" + format_stats(run, model.model, max_llm_calls, seconds), file=out)
    if run.error:
        print(f"Stopped early: {run.error}", file=out)

    if not run.exam_id:
        print("\nNo exam was saved.", file=out)
        return run
    async with services.sessionmaker() as db:
        view = await load_exam(db, run.exam_id)
    print("\nDeveloper view, answers included (a student only sees these after grading):\n", file=out)
    print(*(format_exam(view) if view else [f"Exam {run.exam_id} not found."]), sep="\n", file=out)
    return run


@asynccontextmanager
async def open_services(settings: Settings):
    """The real database, Qdrant and Gemini embeddings, closed afterwards."""
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    qdrant = AsyncQdrantClient(url=settings.qdrant_url)
    try:
        yield AgentServices(
            sessionmaker=async_sessionmaker(engine, expire_on_commit=False),
            vectors=VectorStore(qdrant, settings.collection_name, settings.embed_dimensions),
            embedder=GeminiEmbedder(settings),
        )
    finally:
        await qdrant.close()
        await engine.dispose()


async def _list(settings: Settings) -> None:
    async with open_services(settings) as services, services.sessionmaker() as db:
        rows = await list_sessions(db)
    print(*format_sessions(rows), sep="\n")


async def _write(settings: Settings, session_id: str, request: str, model: BaseLlm, max_llm_calls: int) -> None:
    async with open_services(settings) as services:
        await write_exam(
            services,
            settings,
            session_id=session_id,
            request=request,
            model=model,
            max_llm_calls=max_llm_calls,
            out=sys.stdout,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=SCRIPT, description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("session_id", nargs="?", help="leave out to list sessions")
    parser.add_argument("request", nargs="?", help='what to write, e.g. "5 questions on heaps, mostly multiple choice"')
    parser.add_argument("--model", help="model for this run (default: EXAM_MODEL)")
    parser.add_argument("--max-calls", type=int, help="most model calls in this run (default: EXAM_MAX_LLM_CALLS)")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):  # slide text can hold any character; don't crash on Windows
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "google_genai", "google_adk"):
        logging.getLogger(noisy).setLevel(logging.ERROR)

    settings = get_settings()
    try:
        if args.session_id is None:
            asyncio.run(_list(settings))
            return 0
        if not (args.request or "").strip():
            raise ScriptError('Say what to write, e.g. "5 questions on split brain, mostly multiple choice".')
        if args.max_calls is not None and args.max_calls < 1:
            raise ScriptError("--max-calls must be at least 1.")
        model = load_model(args.model or settings.exam_model, settings)
        max_llm_calls = args.max_calls or settings.exam_max_llm_calls
        asyncio.run(_write(settings, args.session_id, args.request.strip(), model, max_llm_calls))
    except ScriptError as exc:
        print(f"Error: {exc}")
        return 1
    except OSError as exc:  # e.g. ConnectionRefusedError
        print(f"Error: can't reach the database or Qdrant ({exc}). Are the data stores up? (docker compose up -d)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
