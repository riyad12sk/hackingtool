"""Background worker for non-invasive dashboard maintenance jobs only.

No shell, subprocess, arbitrary URL, target scanner, installer, or tool runner is
exposed here. Job types are a fixed allowlist and operate only on local metadata.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

from sqlalchemy import select, update

from webapp.catalog import catalog_summary
from webapp.db import ActivityLog, SessionLocal, WorkerJob

log = logging.getLogger("hackingtool.dashboard.worker")
_stop = threading.Event()
_thread: threading.Thread | None = None

ALLOWED_JOBS = {"catalog_audit", "health_check"}


def _execute(job_type: str) -> str:
    if job_type == "catalog_audit":
        summary = catalog_summary()
        return json.dumps({
            "message": "Catalog metadata audit completed",
            **summary,
        }, ensure_ascii=False)
    if job_type == "health_check":
        return json.dumps({
            "message": "Worker is healthy",
            "worker": "online",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    raise ValueError("Unsupported worker job")


def _claim_one() -> int | None:
    # The conditional UPDATE is the atomic claim; competing workers cannot both
    # transition the same pending job to running.
    with SessionLocal() as db:
        candidate = db.scalar(
            select(WorkerJob.id)
            .where(WorkerJob.status == "pending")
            .order_by(WorkerJob.id.asc())
            .limit(1)
        )
        if candidate is None:
            return None
        result = db.execute(
            update(WorkerJob)
            .where(WorkerJob.id == candidate, WorkerJob.status == "pending")
            .values(status="running", started_at=datetime.now(timezone.utc))
        )
        db.commit()
        return candidate if result.rowcount == 1 else None


def _process(job_id: int) -> None:
    with SessionLocal() as db:
        job = db.get(WorkerJob, job_id)
        if job is None:
            return
        try:
            if job.job_type not in ALLOWED_JOBS:
                raise ValueError("Unsupported worker job")
            job.result = _execute(job.job_type)
            job.status = "completed"
            job.error = ""
            event = f"worker_job_completed:{job.job_type}"
        except Exception as exc:
            job.status = "failed"
            job.error = str(exc)[:1000]
            event = f"worker_job_failed:{job.job_type}"
            log.exception("Worker job %s failed", job_id)
        job.finished_at = datetime.now(timezone.utc)
        db.add(ActivityLog(actor="worker", event=event, ip_address="local", details=f"job_id={job_id}"))
        db.commit()


def _loop() -> None:
    while not _stop.is_set():
        try:
            job_id = _claim_one()
            if job_id is None:
                _stop.wait(2)
            else:
                _process(job_id)
        except Exception:
            log.exception("Worker loop error")
            _stop.wait(3)


def start_worker() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="dashboard-worker", daemon=True)
    _thread.start()


def stop_worker() -> None:
    _stop.set()


def worker_is_alive() -> bool:
    return bool(_thread and _thread.is_alive())
