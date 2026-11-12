"""Celery app: Redis is the broker (the job queue); the worker process runs the tasks.

Start a worker from the backend/ folder (on Windows Celery needs the single-process `solo` pool):
    uv run celery -A app.worker.celery_app worker -Q ingest -P solo --loglevel=info

The API only *sends* jobs (enqueue_ingest); it never imports or runs the task code.
"""

import uuid

from celery import Celery

from app.config import get_settings

INGEST_QUEUE = "ingest"
INGEST_TASK = "ingest_document"

celery_app = Celery("examstudy", broker=get_settings().redis_url, include=["app.worker.tasks"])
celery_app.conf.update(
    # At-least-once delivery: a job is removed from the queue only after it finishes, so a
    # worker that crashes mid-job doesn't lose it. Tasks must therefore be safe to run twice.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,  # take one job at a time
    task_default_queue=INGEST_QUEUE,
    task_ignore_result=True,  # results live in Postgres (documents.status), not in Celery
    broker_connection_retry_on_startup=True,
)


def enqueue_ingest(document_id: uuid.UUID) -> None:
    """Put an ingestion job on the queue (called by the API after an upload or a retry)."""
    celery_app.send_task(INGEST_TASK, args=[str(document_id)], queue=INGEST_QUEUE)
