"""Single-threaded durable job coordinator."""

from __future__ import annotations

from collections.abc import Callable
import fcntl
from pathlib import Path
import shutil
import logging
import sqlite3
import time
from threading import Event, Lock, Thread
from uuid import uuid4

from .models import InvalidTransition, JobRecord, JobState
from .repository import JobRepository


class WorkerStopping(BaseException):
    """Checkpoint shutdown, distinct from a user cancellation."""


class JobRunner:
    def __init__(
        self,
        repository: JobRepository,
        workflow_factory: Callable[[], object],
        *,
        lease_seconds: int = 30,
        poll_seconds: float = 0.5,
        uploads_directory: Path | None = None,
        max_recoveries: int = 3,
    ) -> None:
        self.repository = repository
        self.workflow_factory = workflow_factory
        self.lease_seconds = max(1, lease_seconds)
        self.poll_seconds = max(0.01, poll_seconds)
        self.uploads_directory = uploads_directory
        self.max_recoveries = max(1, max_recoveries)
        self.runner_id = uuid4().hex
        self._stop = Event()
        self._wake = Event()
        self._thread: Thread | None = None
        self._active_lock = Lock()
        self._active_id: str | None = None
        self._active_cancel: Event | None = None
        self._active_configuration = None
        self._last_tick = None
        self._last_error = None
        self._coordinator = False

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(target=self._supervised_loop, name="paper-jobs", daemon=True)
        self._thread.start()

    def stop(self, timeout: float | None = 30) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                raise RuntimeError("后台论文任务未能及时停止")
        self._thread = None

    def wake(self) -> None:
        self._wake.set()

    def cancel(self, job_id: str) -> JobRecord:
        job = self.repository.request_cancel(job_id)
        with self._active_lock:
            if self._active_id == job_id and self._active_cancel is not None:
                self._active_cancel.set()
        self._wake.set()
        return job

    def status(self):
        alive = bool(self._thread and self._thread.is_alive())
        age = None if self._last_tick is None else max(0, time.monotonic()-self._last_tick)
        healthy = alive and self._last_error is None and (age is None or age < max(60, self.poll_seconds*3, self.lease_seconds*3))
        return {'status': 'ok' if healthy else 'error', 'alive': alive,
                'coordinator': self._coordinator, 'active_job': self._active_id,
                'configuration': self._active_configuration,
                'last_heartbeat_seconds': age, 'error_type': self._last_error}

    def _supervised_loop(self):
        while not self._stop.is_set():
            try:
                self._loop()
            except Exception as error:
                self._last_error = type(error).__name__
                logging.getLogger(__name__).error('job_worker_recovering error_type=%s', self._last_error)
                self._stop.wait(min(2, max(.1, self.poll_seconds)))

    def _loop(self) -> None:
        lock_path = self.repository.database.with_suffix(".runner.lock")
        with lock_path.open("a") as coordinator_lock:
            while not self._stop.is_set():
                try:
                    fcntl.flock(coordinator_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._coordinator = True
                    break
                except BlockingIOError:
                    self._last_tick = time.monotonic()
                    self._wake.wait(self.poll_seconds)
                    self._wake.clear()
            else:
                return
            try:
                self._cleanup_uploads()
                while not self._stop.is_set():
                    try:
                        self.repository.recover_expired(max_recoveries=self.max_recoveries)
                        job = self.repository.claim_next(
                            self.runner_id, lease_seconds=self.lease_seconds
                        )
                    except (sqlite3.Error, OSError):
                        self._last_error = "database_unavailable"
                        logging.getLogger(__name__).error("job_queue_unavailable")
                        self._stop.wait(2)
                        continue
                    self._last_tick = time.monotonic()
                    self._last_error = None
                    if job is None:
                        self._wake.wait(self.poll_seconds)
                        self._wake.clear()
                        continue
                    self._execute(job)
            finally:
                self._coordinator = False
                fcntl.flock(coordinator_lock, fcntl.LOCK_UN)

    def _execute(self, job: JobRecord) -> None:
        cancelled = Event()
        heartbeat_stop = Event()
        with self._active_lock:
            self._active_id = job.id
            self._active_cancel = cancelled
        heartbeat = Thread(
            target=self._heartbeat,
            args=(job.id, heartbeat_stop),
            name="paper-jobs-heartbeat",
            daemon=True,
        )
        heartbeat.start()
        try:
            workflow = self.workflow_factory()
            self._active_configuration = getattr(workflow, 'configuration', None)
            def observe():
                if cancelled.is_set(): return True
                if self._stop.is_set(): raise WorkerStopping()
                return False
            workflow.run(job, observe)
        except WorkerStopping:
            self.repository.release(job.id, self.runner_id)
        except BaseException as error:
            logging.getLogger(__name__).error("job_failed job_id=%s library=%s paper=%s error_type=%s",job.id,job.library_id,job.paper_id,type(error).__name__)
            current = self.repository.get(job.id)
            if current is not None and current.state is JobState.RUNNING:
                try:
                    if cancelled.is_set() or current.cancel_requested:
                        self.repository.cancel(job.id)
                    else:
                        self.repository.fail(job.id, str(error))
                except InvalidTransition:
                    pass
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=max(1, self.lease_seconds))
            with self._active_lock:
                self._active_id = None
                self._active_cancel = None
                self._active_configuration = None

    def _heartbeat(self, job_id: str, stopped: Event) -> None:
        interval = max(0.1, self.lease_seconds / 3)
        while not stopped.wait(interval):
            try:
                self.repository.heartbeat(
                    job_id,
                    self.runner_id,
                    lease_seconds=self.lease_seconds,
                )
                self._last_tick = time.monotonic()
                self._last_error = None
            except InvalidTransition:
                with self._active_lock:
                    if self._active_cancel is not None:self._active_cancel.set()
                return
            except sqlite3.Error:
                self._last_error = "database_unavailable"
                logging.getLogger(__name__).error("job_heartbeat_database_error job_id=%s",job_id)

    def _cleanup_uploads(self) -> None:
        root = self.uploads_directory
        if root is None or not root.is_dir():
            return
        retained = {
            upload_name.split("/", 1)[0]
            for upload_name in self.repository.retryable_upload_names()
        }
        for child in root.iterdir():
            if child.is_symlink():
                continue
            if child.name in retained and child.is_dir():
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            elif child.name.startswith("."):
                child.unlink(missing_ok=True)
