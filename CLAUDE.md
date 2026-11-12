# Exam Study Agent

A multi-user web app where students upload lecture slides and past exams, then chat with an AI agent that writes mock exams from those materials and grades their answers.

Built as a portfolio project. Design choices should be defensible in an interview without being overkill: the expected load is a handful of users at once.

**Status:** steps 1 (foundation) and 2 (ingestion) done; step 3 next (see [Build order](#build-order)).

---

## Tech stack

| Layer | Choice |
|---|---|
| Frontend | Next.js (React) |
| API | FastAPI (async), SSE for streaming |
| Agents | Google ADK (`LlmAgent`, `AgentTool`), Gemini via `GOOGLE_API_KEY` |
| Background jobs | Celery with Redis as the broker; Flower dashboard |
| App database | Postgres + SQLAlchemy + Alembic migrations |
| Vector search | Qdrant (dense + sparse hybrid search) |
| File storage | S3-compatible object storage: SeaweedFS (see note below) |
| Cache / locks / progress | Redis |
| Deploy | Docker Compose, locally and on one cloud VM behind Caddy (auto HTTPS) |

**Why SeaweedFS, not MinIO:** MinIO's free community edition was archived in 2026 and its official Docker image can no longer be pulled. SeaweedFS is Apache-2.0 licensed, runs as a single container and speaks the S3 API. The app talks to storage only through an S3 client in `services/`, so the store can be swapped by changing config.

---

## Core concepts

- **Course** (e.g. "EECS 281") belongs to one user. It holds **sessions** and the course-wide **topic scores**.
- **Session** (e.g. "Midterm 1 prep") belongs to a course and **owns its files**.
  - `setup` status: the user uploads and deletes files.
  - **Start session** is allowed only when the session has at least one file and every file is `ready` (the UI disables the button until then; the API re-checks). It sets status to `active`, which **locks the file list for good**.
  - `active` status: chat, exams and grading are allowed; uploads and deletes are rejected (HTTP 409).
  - To use different files, the user creates a **new session, which starts empty**. Files are never shared between sessions.
- **Topics** are extracted from each document and merged into the course's topic list (matched by normalized name). Topic scores are per course, so weak spots carry over between sessions.
- **Exam**: created by ExamWriter inside a session. **Attempt**: one submission of answers to an exam, graded by Grader.
- **Out of scope for v1:** study tips / tutoring (explaining course content). StudyCoach only manages exams and reports results.

---

## Architecture

```
 ┌──────────┐  HTTPS / SSE  ┌────────────────────────┐  enqueue   ┌───────────────────────────┐
 │ Next.js  │◀────────────▶ │ FastAPI (api)          │──────────▶ │ Redis                     │
 │ frontend │               │ • auth, CRUD, uploads  │            │ • Celery broker (`ingest`) │
 └──────────┘               │ • chat: ADK agents     │◀── SSE ─── │ • progress pub/sub        │
                            │   StudyCoach           │  progress  │ • chat locks, rate limits │
                            │    ├─ ExamWriter       │            │ • cache                   │
                            │    └─ Grader           │            └─────────────┬─────────────┘
                            └───┬──────────┬─────────┘                          │ consume
                                │          │                    ┌───────────────▼─────────────┐
                                │          │                    │ Celery worker (`ingest`)    │
                                │          │                    │ parse → OCR → chunk → embed │
                                ▼          ▼                    └───┬──────────┬──────────┬───┘
                        ┌────────────┐ ┌────────┐ ◀─────────────────┘          │          │
                        │ Postgres   │ │ Qdrant │                  ┌───────────▼──┐ ┌─────▼────┐
                        │ app data + │ │ chunks │                  │ Object store │ │ Gemini   │
                        │ ADK chats  │ └────────┘                  │ raw files    │ │ API      │
                        └────────────┘                             └──────────────┘ └──────────┘
```

**Rule of thumb:** slow, non-interactive work (file ingestion) goes through Celery. Agent work runs **in the API process during the chat turn**, and the chat is locked until it finishes. That was a deliberate product choice: the user should not chat while an exam is being written.

### Services (docker-compose)

| Service | Responsible for | Never does |
|---|---|---|
| `web` | Screens, upload UI, chat UI, exam taking | LLM calls |
| `api` | Auth, CRUD, uploads to object storage, enqueueing ingestion, running agents, SSE streams | Parsing, OCR or embedding files |
| `worker` | Celery consumer on queue `ingest` | HTTP |
| `postgres` | Source of truth for everything except raw files and vectors | Vector search |
| `qdrant` | Chunk vectors + filter payload. **Rebuildable** from the `pages` table | Being the source of truth |
| `seaweedfs` | Raw uploaded files (S3 API) | — |
| `redis` | Broker, progress pub/sub, locks, rate limits, cache | Holding anything that can't be lost |
| `flower` | Celery dashboard | — |
| `caddy` | HTTPS reverse proxy (cloud deploy only) | — |

---

## Data model (Postgres)

```
users          id, username (unique, 3–30 chars), password_hash, created_at
courses        id, user_id → users, name, created_at
study_sessions id, course_id → courses, name, status (setup | active), started_at, created_at
documents      id, session_id → study_sessions, filename, storage_key, kind (lecture | past_exam | notes),
               status (queued | processing | ready | failed), error, page_count, size_bytes, created_at
pages          id, document_id → documents, page_number, text, source (text | ocr)
topics         id, course_id → courses, name, normalized_name (unique per course)
document_topics document_id, topic_id, page_start, page_end
topic_scores   course_id, topic_id, points_earned, points_possible, updated_at
exams          id, session_id → study_sessions, title, request (user's prompt), questions (JSONB, see below), created_at
attempts       id, exam_id → exams, answers (JSONB), status (submitted | grading | graded | grading_failed),
               score, max_score, created_at, graded_at
attempt_grades attempt_id, question_id, points, feedback, citations (JSONB)   -- one row per question, upserted
(ADK session tables, managed by ADK's DatabaseSessionService, one ADK session per study session)
```

- The table is `study_sessions`, not `sessions`, because ADK's `DatabaseSessionService` creates its own `sessions` table. URLs still say `/sessions/...`.
- Ownership chain: `users → courses → study_sessions → documents/exams → attempts`. Every query checks it. Someone else's item returns **404, the same as a missing one**, so IDs can't be probed (`OwnedCourse` / `OwnedSession` dependencies in `api/deps.py`).
- `pages` keeps extracted text so Qdrant can be rebuilt (e.g. after changing embedding model) without re-uploading or re-running OCR.
- Deleting a course or session cascades to rows, object-store files and Qdrant points (`delete by filter session_id`).

### Exam format (`exams.questions`)

```json
[{
  "id": "q1",
  "type": "multiple_choice | short_answer | long_answer",
  "topic": "Heaps",
  "points": 2,
  "prompt": "…",
  "choices": ["…"],                 // multiple_choice only
  "answer": "…",                    // correct choice or model answer
  "rubric": ["…"],                  // grading criteria for written answers
  "sources": [{"document_id": "…", "page": 12}]
}]
```

Validated by a Pydantic model in `save_exam`. **The answer key (`answer`, `rubric`) is never sent to the browser before the attempt is graded.**

---

## Knowledge base (Qdrant)

- One collection per embedding model and vector size, e.g. `chunks__gemini-embedding-2__768`. The active collection name comes from config. Re-embedding = build a new collection from `pages`, then switch config.
- Embeddings: `gemini-embedding-2` at 768 dimensions, cosine distance. This model takes the task as a text prefix: chunks are embedded as `title: <file name> | text: <chunk>`, queries as `task: search result | query: <query>`.
- Each point: dense vector + sparse (keyword) vector + payload `{user_id, course_id, session_id, document_id, kind, page_number, chunk_index, topics[], text}`.
- Payload indexes on `user_id`, `session_id`, `kind`.
- **Every search filters on `user_id` + `session_id`.** The filter is applied inside the search service, never chosen by the LLM.
- **Hybrid search** (dense + sparse, fused) so exact terms like "Dijkstra" or "Theorem 3.2" are found as well as paraphrases.
- Point IDs are deterministic: `uuid5(document_id:page:chunk_index)`. Writes are **upserts**, so re-running ingestion never duplicates.

### Chunking

| Input | Unit |
|---|---|
| PPTX | 1 slide = 1 chunk (slide text + speaker notes) |
| Lecture/notes PDF | Per page; long pages split recursively (~800 chars, ~80 overlap) |
| Past exam | 1 question = 1 chunk (so ExamWriter can retrieve real questions by topic) |
| Scanned / image-only pages | Gemini vision OCR → text, then chunked as above |

Accepted uploads: PDF and PPTX, max 50 MB each (`MAX_UPLOAD_MB`). The API checks the extension **and** the file's first bytes (`%PDF-`, or the ZIP signature for PPTX): 415 for other types, 413 if too large, 422 if empty. Files are stored at `sessions/<session_id>/<document_id>.<ext>`, so a session's files are deleted by prefix. The cloud deploy should also cap request bodies in Caddy, since the app only sees the size after the upload has been received.

---

## Ingestion pipeline (Celery)

Triggered by `POST /sessions/{id}/documents`: the API saves the file to object storage, creates a `documents` row (`queued`), puts an `ingest_document(document_id)` job on the queue (only the ID travels through Redis; the worker downloads the file itself), and returns 202 at once.

`ingest_document` (queue `ingest`):
1. Parse PDF/PPTX → pages. Detect pages with no text layer.
2. OCR those pages with Gemini vision.
3. Save `pages` rows.
4. Extract topics: **one** LLM call per document → topics with page ranges → merge into course topics.
5. Chunk (rules above), embed, upsert into Qdrant.
6. Mark the document `ready`.

Progress events are published at each step to Redis channel `progress:{user_id}` (pub/sub, `services/progress.py`) and forwarded to the browser by `GET /sessions/{id}/events` (SSE):
- Events: `{"document_id", "session_id", "stage", ...}` with stage `parsing` → `indexing` (+`page_count`) → `ready` (+`page_count`, `chunk_count`), or `failed` (+`error`). Step 3 adds `reading scans`.
- The stream first sends a `snapshot` event (session status + every document, from Postgres), then `progress` events for that session only, and a `: ping` comment every 15 s when idle. Pub/sub stores nothing, so the snapshot is what makes late or reconnecting browsers correct; Postgres stays the source of truth.
- The stream subscribes before reading the snapshot, so no event falls in the gap. Publishing never fails a job.

Celery settings and rules:
- `acks_late=True`, `task_reject_on_worker_lost=True`, `worker_prefetch_multiplier=1` (at-least-once delivery, one job at a time per worker process).
- **Tasks must be idempotent** (they may run twice).
- Retry Gemini 429/5xx/timeouts with exponential backoff (2, 4, 8, 16, 32 s; max 5), **per API call** (`services/gemini.py`), so a rate limit halfway through a document doesn't redo the batches that already succeeded. When a 429 says how long to wait (`retryDelay`, e.g. until the free tier's per-minute quota resets), wait that long instead (capped at 90 s). After that: `failed` + error message; the user can `POST /documents/{id}/retry`.
- If the queue can't be reached at upload time, the document is marked `failed` ("press Retry") instead of staying `queued` forever.
- A document deleted while it's being processed is detected at the end, and any chunks just written for it are removed.
- Corrupt or password-protected files fail immediately without retry.

---

## Agents (Google ADK)

```
StudyCoach (manager, talks to the user)
 ├── ExamWriter  (AgentTool)
 └── Grader      (AgentTool)
```

ExamWriter and Grader are wrapped as **`AgentTool`s**, not `sub_agents`. StudyCoach calls them and waits, and the result always returns to StudyCoach (with `sub_agents`, a transferred agent can stay in control). They are true agents: the LLM chooses which tools to call, how often, and in what order. No fixed pipelines.

Agents run in the `api` process inside the chat request. The chat turn streams ADK events over SSE (status lines such as "Searching lecture material on heaps…", text, and an `exam_ready` / `grading_done` event).

### StudyCoach

Understands the request, asks follow-up questions when needed (topics, number and type of questions), calls the right specialist, and summarizes results. Does **not** teach course content in v1.

| Tool | Purpose |
|---|---|
| `ExamWriter` | Write a mock exam |
| `Grader` | Grade a submitted attempt |
| `list_course_topics()` | Course topics and how much material covers each |
| `get_weak_topics()` | Lowest-scoring topics for this course |
| `get_attempt_results(attempt_id?)` | Scores/feedback of past attempts |

### ExamWriter

| Tool | Purpose |
|---|---|
| `search_course_materials(query, kind?)` | Hybrid search in this session's chunks; returns text + citations |
| `get_past_exam_questions(topic)` | Real past-exam questions on a topic (style/difficulty reference) |
| `list_course_topics()` | For even topic coverage |
| `get_weak_topics()` | Default focus when the request is vague |
| `save_exam(exam)` | Validates and saves atomically; **returns validation errors so the agent can fix and retry** |

If materials are thin for a requested topic, it writes fewer questions and says so.

### Grader

- **Multiple-choice questions are graded by plain code** before the agent runs.
- The agent grades written answers only:

| Tool | Purpose |
|---|---|
| `get_attempt(attempt_id)` | Questions, rubric and the student's answers (ownership-checked) |
| `search_course_materials(query)` | Check an answer against the source material |
| `record_grade(question_id, points, feedback, citations)` | Upsert one question's grade; rejects points outside `0..max` |

- After grading, **plain code** sums the score and updates `topic_scores`.
- Grading is triggered when the user presses **Submit**: `POST /exams/{id}/attempts` saves the answers, then the UI sends a structured chat turn `{"action": "grade", "attempt_id": …}` (not free text).

### Agent rules

- **Identity comes from session state, never from tool arguments.** `user_id`, `course_id` and `session_id` are put in ADK session state by the server; tools read them from `tool_context.state`. The LLM can't choose whose data to read.
- Cap LLM calls per turn with ADK `RunConfig(max_llm_calls=…)` (~25). On hitting the cap: "I couldn't finish that; try a smaller request."
- Agents touch the outside world **only through their tools**. `agents/` imports services; services never import agents. This keeps agents testable and splittable later.
- Model names come from config (`COACH_MODEL`, `EXAM_MODEL`, `GRADER_MODEL`). Gemini by default; ADK's `LiteLlm` wrapper allows other providers without code changes.
- Prompts live in `backend/app/agents/prompts/` as separate files, not inline strings.

---

## API

All endpoints except auth require a JWT in a secure, httpOnly cookie. All check ownership.

```
Auth
  POST   /auth/register                  {username, password} → creates account, sets cookie
  POST   /auth/login                     {username, password} → sets cookie
  POST   /auth/logout
  GET    /auth/me                        current user, or 401

Courses
  GET    /courses
  POST   /courses                        {name}
  GET    /courses/{id}
  DELETE /courses/{id}                   cascades
  GET    /courses/{id}/topics            topics + this course's scores

Sessions
  GET    /courses/{id}/sessions
  POST   /courses/{id}/sessions          {name} → status=setup
  GET    /sessions/{id}
  DELETE /sessions/{id}                  cascades
  POST   /sessions/{id}/start            409 unless ≥1 document and every document is ready → status=active

Documents (only while session status = setup, else 409)
  POST   /sessions/{id}/documents        multipart; 202 {document_id, status: queued}
  GET    /sessions/{id}/documents
  DELETE /documents/{id}
  POST   /documents/{id}/retry           only if failed
  GET    /sessions/{id}/events           SSE: ingestion progress

Chat (only while session status = active)
  POST   /sessions/{id}/chat             {message} or {action}; SSE stream of the agent turn
  GET    /sessions/{id}/chat/history

Exams
  GET    /sessions/{id}/exams
  GET    /exams/{id}                     questions WITHOUT answer key
  POST   /exams/{id}/attempts            {answers} → {attempt_id}
  GET    /attempts/{id}                  score, per-question feedback, answer key (only once graded)
```

### Concurrency rules

- **Session row lock for file changes:** upload, delete, retry and start take `SELECT … FOR UPDATE` on the session row (`lock_session` in `api/deps.py`) and re-check the status while holding it, so a file can't be added as the session is being started (check-then-act race). Uploads send the file to storage *before* taking the lock, so the lock is held for milliseconds; if the re-check fails, the stored file is deleted.
- **One agent turn at a time per session:** Redis lock `chatlock:{session_id}` (`SET NX` with 5-minute TTL). A second request gets 409 "busy". The TTL means a crashed server can't leave a chat locked forever.
- **Rate limits** in Redis per user (e.g. 20 agent turns/hour, upload limits) → 429 with a friendly message. Stricter limits for the demo account.
- Run the API with several Uvicorn workers; agent turns are async and mostly wait on network I/O.

---

## Frontend screens

| Screen | Content |
|---|---|
| Login / Register | Username + password |
| Courses | List, "+ New course" |
| Course | Sessions list, "+ New session", topic list colored by score |
| Session (setup) | Drag-and-drop upload, file list with live progress (`queued → parsing → reading scans → indexing → ready` / failed + Retry), delete, **Start session** (disabled until all ready) |
| Session (active) | Locked file list; chat with StudyCoach (input disabled during an agent turn, status lines shown); "Mock exams" tab with scores |
| Take exam | Questions, radio buttons / text boxes, Submit |
| Results | Score, ✅/❌ per question, feedback, correct answer, links to source slides/pages |

---

## Error handling

| Situation | Behavior |
|---|---|
| Gemini 429 / 5xx / timeout during ingestion | Retried per API call with backoff (or Google's `retryDelay`), then `failed` + Retry button |
| Corrupt or password-protected file | Fail immediately, clear message |
| Worker crash mid-job | Redelivered (`acks_late`); idempotent upserts prevent duplicates |
| Error during an agent turn | SSE `error` event, chat unlocks, nothing half-saved |
| `max_llm_calls` reached | Polite "try a smaller request" message |
| Grader fails midway | Grades so far are kept; attempt `grading_failed`; Regrade re-runs (upserts) |
| Not enough material | ExamWriter writes fewer questions and says why |
| Too many requests | 429 from the Redis rate limiter |

---

## Testing

| Kind | Covers | Runs |
|---|---|---|
| Unit (`tests/unit`) | Chunkers (slide, page, past-exam question split), deterministic IDs, exam schema validation, MCQ grading, topic score math | Every push (GitHub Actions) |
| API (`tests/api`) | Auth; **user A can't access user B's data**; session lock 409s; answer key hidden before grading; chat lock | Every push, against Postgres/Redis/Qdrant in Docker |
| Test isolation | API tests use database `study_test`, Redis DB 15, bucket `<S3_BUCKET>-test` and Qdrant collection `<collection>__test` (set in `tests/conftest.py`), so they never touch dev data. They never call Gemini (fake embedder) or the real job queue | — |
| Agent evals (`tests/evals`) | Monopoly sample course: ExamWriter produces a valid exam whose sources point to real chunks; Grader scores known-good answers high and known-bad answers low; LLM-as-judge for quality | Manually (costs API calls) |

---

## Configuration (`.env`, never committed)

```
GOOGLE_API_KEY=
COACH_MODEL=            # Gemini model names from Google AI Studio
EXAM_MODEL=
GRADER_MODEL=
OCR_MODEL=
EMBED_MODEL=gemini-embedding-2
EMBED_DIMENSIONS=768
QDRANT_COLLECTION=      # derived from EMBED_MODEL + EMBED_DIMENSIONS if empty
POSTGRES_PASSWORD=
DATABASE_URL=postgresql+asyncpg://study:${POSTGRES_PASSWORD}@postgres:5432/study
REDIS_URL=redis://redis:6379/0
QDRANT_URL=http://qdrant:6333
S3_ENDPOINT=  S3_ACCESS_KEY=  S3_SECRET_KEY=  S3_BUCKET=
JWT_SECRET=
```

`.env.example` lists every key with no secret values. **Never hard-code or commit keys.**

---

## Deployment

- **Local:** `docker compose up -d` → web at http://localhost:3000, Qdrant dashboard at http://localhost:6333/dashboard, Flower at http://localhost:5555.
- **Local, before the app services are containerized:** `docker compose up -d` runs only the data stores. From `backend/`, run the API with `uv run uvicorn app.main:app --reload` and the worker with `uv run celery -A app.worker.celery_app worker -Q ingest -P solo --loglevel=info` (`-P solo` because Celery's default process pool doesn't work on Windows). SeaweedFS's file browser is at http://localhost:8888/buckets/ (local only).
- **Cloud demo:** one VM (~4 GB RAM minimum) running the same compose file plus `caddy` for HTTPS; domain optional (a free subdomain works).
- **Demo account** preloaded with the Monopoly sample course (`data/samples/`).
- Nightly `pg_dump` backup. Qdrant is rebuildable from `pages`.

### Costs

- All software in the stack is free/open source. Running locally costs nothing beyond API usage.
- **Gemini API** is the main variable cost: embeddings (per chunk), OCR (per scanned page, the most expensive ingestion step), topic extraction (per document), every chat turn, ExamWriter (many calls per exam), Grader (per written answer), evals. The AI Studio free tier has rate limits, and Google may use free-tier inputs to improve its products: don't upload material you can't share while on the free tier.
- **Public demo risk:** strangers spending your API quota. Keep the Redis rate limits, stricter demo limits, and a budget alert/cap on the Google account.
- Cloud VM ~$5–20/month (or free-tier/credit offers; verify current terms). Domain optional (~$10–15/year).

---

## Repository layout (target)

```
├── CLAUDE.md
├── docker-compose.yml
├── .env.example
├── Caddyfile
├── backend/
│   ├── pyproject.toml
│   ├── alembic/
│   ├── app/
│   │   ├── main.py          FastAPI app
│   │   ├── config.py        settings from .env
│   │   ├── db/              SQLAlchemy models, session
│   │   ├── api/             routers: auth, courses, sessions, documents, chat, exams
│   │   ├── services/        object storage, Qdrant search, Redis (progress, locks, rate limits)
│   │   ├── ingest/          parsers (pdf, pptx), ocr, chunking, embedding, topics
│   │   ├── agents/          study_coach.py, exam_writer.py, grader.py, tools/, prompts/
│   │   ├── grading/         MCQ grading, topic score math (plain code)
│   │   └── worker/          celery_app.py, tasks.py
│   └── tests/               unit/, api/, evals/
├── frontend/                Next.js app
└── data/samples/            Monopoly files (tests + demo)
```

One repository on purpose: agents share models, services and config with the API and run in the same process. Keep the boundary clean (agents use services only through tools) so they could be split out later.

---

## Build order

Each step ends with something runnable.

- [x] **1. Foundation:** docker-compose (Postgres, Redis, Qdrant, object storage), FastAPI skeleton, config, Alembic, username/password auth.
- [x] **2. Ingestion:** courses, sessions (setup/active), upload → Celery → PDF/PPTX text → chunks → Qdrant; progress SSE; session start/lock rules.
- [ ] **3. Rich ingestion:** OCR for scanned pages, topic extraction, past-exam question splitting, hybrid search.
- [ ] **4. ExamWriter** + its tools + exam schema (try it via a script or `adk web`).
- [ ] **5. Grader**, MCQ grading, attempts, topic scores.
- [ ] **6. StudyCoach** + streaming chat endpoint + chat lock + rate limits.
- [ ] **7. Frontend** screens.
- [ ] **8. Quality:** unit/API tests in GitHub Actions, agent evals, demo account.
- [ ] **9. Cloud deploy:** VM + Caddy + domain + backups.

Backend first (1–6) so the hard parts work before any UI exists.

---

## Open items

- **Models:** embeddings use `gemini-embedding-2` (768 dims). Still to choose: Gemini models for coach / exam / grader / OCR.
- **Sample data:** `data/samples/` doesn't exist yet; add the Monopoly sample files (PDF/Markdown) before agent evals and the demo account.
