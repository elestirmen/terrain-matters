"""A durable job queue in the database.

The pipeline and the optimizer used to run inside the web process, on a
single-slot thread pool guarded by a module-level variable. That is why
``gunicorn --workers 1`` is mandatory: a second worker would keep its own pool
and its own idea of what is running, so two of them could start the same
publication at once. It also meant a restart lost whatever was in flight, and
progress was only visible to the process that happened to be running it.

Jobs are rows here, and a separate worker process runs them. The web layer only
enqueues and reads, so it can be restarted, scaled, or replaced mid-job.

Claiming uses ``FOR UPDATE SKIP LOCKED``: several workers may poll at once and
each takes a different job without coordinating. The database decides, not a
variable only one process can see.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import uuid
from datetime import timedelta
from typing import Any

from . import db

log = logging.getLogger(__name__)

KINDS = ("pipeline", "optimization")
ACTIVE_STATUSES = ("queued", "running")

# A worker updates its heartbeat while it works. Past this, assume it died: the
# job is failed rather than left running forever, and the slot is freed.
HEARTBEAT_TIMEOUT = timedelta(minutes=10)


class JobError(RuntimeError):
    """Raised when a job cannot be enqueued or claimed."""


class JobAlreadyRunning(JobError):
    """Raised when a job of this kind is already queued or running."""


def worker_id() -> str:
    """Identify this process in the job row, so a stuck job can be traced."""
    return f"{socket.gethostname()}:{os.getpid()}"


def _row_to_job(row) -> dict[str, Any]:
    (
        job_id, kind, status, parameters, progress, result,
        created_at, started_at, finished_at, heartbeat_at, worker,
    ) = row
    return {
        "job_id": str(job_id),
        "kind": kind,
        "status": status,
        "parameters": parameters or {},
        "progress": progress or {},
        "result": result,
        "created_at_utc": created_at.isoformat() if created_at else None,
        "started_at_utc": started_at.isoformat() if started_at else None,
        "finished_at_utc": finished_at.isoformat() if finished_at else None,
        "heartbeat_at_utc": heartbeat_at.isoformat() if heartbeat_at else None,
        "worker_id": worker,
    }


SELECT_COLUMNS = """
    id, kind, status, parameters, progress, result,
    created_at, started_at, finished_at, heartbeat_at, worker_id
"""


def enqueue(kind: str, parameters: dict[str, Any] | None = None, *, requested_by: int | None = None) -> dict[str, Any]:
    """Add a job, or refuse because one of this kind is already in flight.

    The refusal comes from a partial unique index rather than from a check
    followed by an insert, so two processes racing cannot both win.
    """
    if kind not in KINDS:
        raise JobError(f"Bilinmeyen iş türü: {kind}")
    import psycopg

    with db.connection() as conn, conn.cursor() as cursor:
        reclaim_stale(cursor)
        try:
            cursor.execute(
                f"""
                INSERT INTO job (id, kind, parameters, requested_by)
                VALUES (%s, %s, %s::jsonb, %s)
                RETURNING {SELECT_COLUMNS}
                """,
                (uuid.uuid4(), kind, json.dumps(parameters or {}, ensure_ascii=False), requested_by),
            )
        except psycopg.errors.UniqueViolation as exc:
            raise JobAlreadyRunning(f"Başka bir {kind} işi çalışıyor.") from exc
        return _row_to_job(cursor.fetchone())


def get(job_id: str) -> dict[str, Any] | None:
    try:
        parsed = uuid.UUID(str(job_id))
    except (ValueError, AttributeError):
        return None
    with db.connection() as conn, conn.cursor() as cursor:
        cursor.execute(f"SELECT {SELECT_COLUMNS} FROM job WHERE id = %s", (parsed,))
        row = cursor.fetchone()
        return _row_to_job(row) if row else None


def latest(kind: str | None = None) -> dict[str, Any] | None:
    """The most recent job, which is what a status panel shows."""
    with db.connection() as conn, conn.cursor() as cursor:
        if kind:
            cursor.execute(
                f"SELECT {SELECT_COLUMNS} FROM job WHERE kind = %s ORDER BY created_at DESC LIMIT 1",
                (kind,),
            )
        else:
            cursor.execute(f"SELECT {SELECT_COLUMNS} FROM job ORDER BY created_at DESC LIMIT 1")
        row = cursor.fetchone()
        return _row_to_job(row) if row else None


def active(kind: str) -> dict[str, Any] | None:
    with db.connection() as conn, conn.cursor() as cursor:
        reclaim_stale(cursor)
        cursor.execute(
            f"SELECT {SELECT_COLUMNS} FROM job WHERE kind = %s AND status = ANY(%s) LIMIT 1",
            (kind, list(ACTIVE_STATUSES)),
        )
        row = cursor.fetchone()
        return _row_to_job(row) if row else None


def claim(kinds: tuple[str, ...] = KINDS, *, worker: str | None = None) -> dict[str, Any] | None:
    """Take the oldest queued job of any of these kinds, or return None.

    SKIP LOCKED lets several workers poll the same table without blocking each
    other or handing the same job to two of them.
    """
    with db.connection() as conn, conn.cursor() as cursor:
        reclaim_stale(cursor)
        cursor.execute(
            f"""
            UPDATE job SET status = 'running',
                           started_at = now(),
                           heartbeat_at = now(),
                           worker_id = %s
             WHERE id = (
                   SELECT id FROM job
                    WHERE status = 'queued' AND kind = ANY(%s)
                    ORDER BY created_at
                    LIMIT 1
                      FOR UPDATE SKIP LOCKED
             )
            RETURNING {SELECT_COLUMNS}
            """,
            (worker or worker_id(), list(kinds)),
        )
        row = cursor.fetchone()
        return _row_to_job(row) if row else None


def heartbeat(job_id: str, progress: dict[str, Any] | None = None) -> None:
    """Say the worker is still alive, and optionally how far it has got."""
    with db.connection() as conn, conn.cursor() as cursor:
        if progress is None:
            cursor.execute("UPDATE job SET heartbeat_at = now() WHERE id = %s", (uuid.UUID(str(job_id)),))
        else:
            cursor.execute(
                "UPDATE job SET heartbeat_at = now(), progress = %s::jsonb WHERE id = %s",
                (json.dumps(progress, ensure_ascii=False), uuid.UUID(str(job_id))),
            )


def finish(job_id: str, result: dict[str, Any] | None = None, *, failed: bool = False) -> dict[str, Any] | None:
    with db.connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            f"""
            UPDATE job SET status = %s, finished_at = now(), result = %s::jsonb
             WHERE id = %s
            RETURNING {SELECT_COLUMNS}
            """,
            (
                "failed" if failed else "succeeded",
                json.dumps(result, ensure_ascii=False) if result is not None else None,
                uuid.UUID(str(job_id)),
            ),
        )
        row = cursor.fetchone()
        return _row_to_job(row) if row else None


def reclaim_stale(cursor, *, timeout: timedelta = HEARTBEAT_TIMEOUT) -> int:
    """Fail jobs whose worker stopped reporting, freeing the slot.

    Without this a killed worker leaves a row marked running forever and the
    partial unique index blocks every later job of that kind — the file-based
    queue had the same failure, as a lock file nobody cleaned up.
    """
    cursor.execute(
        """
        UPDATE job
           SET status = 'failed',
               finished_at = now(),
               result = jsonb_build_object(
                   'success', false,
                   'stderr', 'İşi yürüten süreç yanıt vermiyor; iş terk edilmiş sayıldı.'
               )
         WHERE status = 'running'
           AND coalesce(heartbeat_at, started_at, created_at) < now() - %s::interval
        """,
        (timeout,),
    )
    if cursor.rowcount:
        log.warning("Terk edilmiş iş sayısı: %s", cursor.rowcount)
    return cursor.rowcount


def prune(*, older_than: timedelta = timedelta(days=30)) -> int:
    """Drop finished jobs nobody will look at again."""
    with db.connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            DELETE FROM job
             WHERE status IN ('succeeded', 'failed', 'cancelled')
               AND finished_at < now() - %s::interval
            """,
            (older_than,),
        )
        return cursor.rowcount


def public_view(job: dict[str, Any] | None) -> dict[str, Any] | None:
    """The fields a public status panel may see: freshness, never parameters or logs."""
    if job is None:
        return None
    return {
        key: job[key]
        for key in ("status", "created_at_utc", "started_at_utc", "finished_at_utc")
        if key in job
    }
