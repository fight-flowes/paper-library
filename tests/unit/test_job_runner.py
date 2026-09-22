from __future__ import annotations

from pathlib import Path
from threading import Event, Lock
import time
from datetime import datetime, timedelta, timezone

from paper_library.jobs import JobRepository, JobState, NewJob
from paper_library.jobs.runner import JobRunner


def add_job(repository: JobRepository, upload_name: str = "abc/source.pdf"):
    return repository.create(
        NewJob("paper.pdf", "whitespace", "topic", "en", False, False, upload_name)
    )


def wait_until(predicate, timeout: float = 2) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached")


def test_two_runners_execute_a_job_once_and_wake_immediately(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    first_repository = JobRepository(database)
    second_repository = JobRepository(database)
    job = add_job(first_repository)
    calls: list[str] = []
    lock = Lock()

    class Workflow:
        def __init__(self, jobs: JobRepository) -> None:
            self.jobs = jobs

        def run(self, claimed, cancelled) -> None:
            with lock:
                calls.append(claimed.id)
            self.jobs.complete(claimed.id)

    first = JobRunner(first_repository, lambda: Workflow(first_repository), poll_seconds=10)
    second = JobRunner(second_repository, lambda: Workflow(second_repository), poll_seconds=10)
    first.start()
    second.start()
    try:
        first.wake()
        second.wake()
        wait_until(lambda: first_repository.get(job.id).state is JobState.COMPLETED)
    finally:
        first.stop()
        second.stop()

    assert calls == [job.id]


def test_heartbeat_prevents_recovery_and_stop_waits_for_active_job(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "jobs.sqlite3")
    job = add_job(repository)
    started = Event()
    release = Event()

    class Workflow:
        def run(self, claimed, cancelled) -> None:
            started.set()
            release.wait(2)
            repository.complete(claimed.id)

    runner = JobRunner(
        repository,
        Workflow,
        lease_seconds=1,
        poll_seconds=0.01,
    )
    runner.start()
    assert started.wait(1)
    time.sleep(1.2)
    assert repository.recover_expired() == 0
    release.set()
    runner.stop()
    assert repository.get(job.id).state is JobState.COMPLETED


def test_cancel_queued_job_is_terminal(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "jobs.sqlite3")
    job = add_job(repository)
    runner = JobRunner(repository, lambda: None, poll_seconds=10)

    runner.cancel(job.id)

    assert repository.get(job.id).state is JobState.CANCELLED


def test_runner_recovers_lease_that_expires_after_startup(tmp_path: Path) -> None:
    class Clock:
        now = datetime(2026, 9, 20, tzinfo=timezone.utc)

        def __call__(self):
            return self.now

    clock = Clock()
    repository = JobRepository(tmp_path / "jobs.sqlite3", clock=clock)
    job = add_job(repository)
    repository.claim_next("dead", lease_seconds=1)

    class Workflow:
        def run(self, claimed, cancelled) -> None:
            repository.complete(claimed.id)

    runner = JobRunner(repository, Workflow, poll_seconds=0.01)
    runner.start()
    try:
        clock.now += timedelta(seconds=2)
        runner.wake()
        wait_until(lambda: repository.get(job.id).state is JobState.COMPLETED)
    finally:
        runner.stop()


def test_startup_cleanup_retains_all_retryable_uploads_beyond_history_limit(
    tmp_path: Path,
) -> None:
    repository = JobRepository(tmp_path / "jobs.sqlite3")
    uploads = tmp_path / "uploads"
    for index in range(201):
        name = f"{index:032x}"
        add_job(repository, f"{name}/source.pdf")
        directory = uploads / name
        directory.mkdir(parents=True)
        (directory / "source.pdf").write_bytes(b"%PDF-job")
    runner = JobRunner(
        repository,
        lambda: None,
        poll_seconds=10,
        uploads_directory=uploads,
    )

    runner.start()
    runner.stop()

    assert len([path for path in uploads.iterdir() if path.is_dir()]) == 201


def test_graceful_stop_requeues_instead_of_cancelling(tmp_path):
    repository=JobRepository(tmp_path/'jobs.sqlite3');job=add_job(repository);started=Event()
    class Workflow:
        def run(self,claimed,cancelled):
            started.set()
            while True:
                cancelled();time.sleep(.01)
    runner=JobRunner(repository,Workflow,poll_seconds=.01)
    runner.start();assert started.wait(1);runner.stop(timeout=2)
    assert repository.get(job.id).state==JobState.QUEUED


def test_worker_survives_database_error_inside_failure_handler(tmp_path,monkeypatch):
    import sqlite3
    repository=JobRepository(tmp_path/'jobs.sqlite3');job=add_job(repository)
    original=repository.get;broken=Event()
    class Flow:
        def run(self,claimed,cancelled):
            broken.set();raise ValueError('task failure')
    def get(key):
        if broken.is_set():
            broken.clear();raise sqlite3.OperationalError('temporary unavailable')
        return original(key)
    monkeypatch.setattr(repository,'get',get)
    runner=JobRunner(repository,Flow,poll_seconds=.01)
    runner.start()
    try:
        wait_until(lambda: runner._last_error is not None)
        wait_until(lambda: runner.status()['status']=='ok')
        assert runner.status()['alive']
    finally:runner.stop()
