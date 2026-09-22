from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, Thread

import pytest

from paper_library.jobs import (
    InvalidTransition,
    JobProgress,
    JobRepository,
    JobStage,
    JobState,
    NewJob,
)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 20, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


def new_job(**changes: object) -> NewJob:
    job = NewJob(
        original_filename="paper.pdf",
        library_id="whitespace",
        subject="related-work",
        language="en",
        enrich=True,
        auto_translate=True,
        upload_name="upload-id/source.pdf",
    )
    return replace(job, **changes)


def test_repository_claims_one_job_and_persists_progress(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "jobs.sqlite3")
    job = repository.create(new_job())

    claimed = repository.claim_next("runner-a", lease_seconds=30)

    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.state is JobState.RUNNING
    assert repository.claim_next("runner-b", lease_seconds=30) is None
    repository.update(
        job.id,
        JobStage.TRANSLATING,
        JobProgress(total=10, cached=2, translated=3, failed=0, pending=5),
    )
    assert repository.get(job.id).progress.translated == 3


def test_expired_job_is_recovered_without_losing_paper_id(tmp_path: Path) -> None:
    clock = Clock()
    repository = JobRepository(tmp_path / "jobs.sqlite3", clock=clock)
    job = repository.create(new_job())
    repository.claim_next("dead-runner", lease_seconds=1)
    repository.update(
        job.id,
        JobStage.TRANSLATING,
        JobProgress(total=5, pending=5),
        paper_id="ws-p0003",
    )

    clock.advance(2)

    assert repository.recover_expired() == 1
    recovered = repository.get(job.id)
    assert recovered.state is JobState.QUEUED
    assert recovered.paper_id == "ws-p0003"
    assert recovered.runner_id is None


def test_invalid_transitions_are_rejected(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "jobs.sqlite3")
    job = repository.create(new_job())

    with pytest.raises(InvalidTransition):
        repository.complete(job.id)

    repository.request_cancel(job.id)
    with pytest.raises(InvalidTransition):
        repository.retry(job.id)


def test_cancel_queued_job_and_request_running_job_cancel(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "jobs.sqlite3")
    queued = repository.create(new_job(upload_name="one/source.pdf"))
    running = repository.create(new_job(upload_name="two/source.pdf"))

    cancelled = repository.request_cancel(queued.id)
    repository.claim_next("runner", lease_seconds=30)
    requested = repository.request_cancel(running.id)

    assert cancelled.state is JobState.CANCELLED
    assert requested.state is JobState.RUNNING
    assert requested.cancel_requested
    assert repository.cancel_requested(running.id)


def test_retry_clears_failure_and_progress_but_retains_artifacts(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "jobs.sqlite3")
    job = repository.create(new_job())
    repository.claim_next("runner", lease_seconds=30)
    repository.update(
        job.id,
        JobStage.TRANSLATING,
        JobProgress(total=20, translated=4, pending=16),
        paper_id="ws-p0099",
    )
    repository.fail(job.id, "secret\ninternal response " + "x" * 800)

    failed = repository.get(job.id)
    assert "\n" not in (failed.error or "")
    assert len(failed.error or "") <= 500

    retried = repository.retry(job.id)
    assert retried.state is JobState.QUEUED
    assert retried.error is None
    assert retried.progress == JobProgress()
    assert retried.paper_id == "ws-p0099"
    assert retried.upload_name == "upload-id/source.pdf"


def test_complete_supports_warnings_and_heartbeat_extends_lease(tmp_path: Path) -> None:
    clock = Clock()
    repository = JobRepository(tmp_path / "jobs.sqlite3", clock=clock)
    job = repository.create(new_job())
    repository.claim_next("runner", lease_seconds=10)
    first_lease = repository.get(job.id).lease_expires_at
    clock.advance(3)

    repository.heartbeat(job.id, "runner", lease_seconds=20)
    assert repository.get(job.id).lease_expires_at > first_lease
    completed = repository.complete(job.id, warnings=True)
    assert completed.state is JobState.COMPLETED_WITH_WARNINGS
    assert completed.stage is JobStage.COMPLETED


def test_two_repositories_cannot_claim_the_same_job(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    first = JobRepository(database)
    second = JobRepository(database)
    job = first.create(new_job())
    barrier = Barrier(3)
    claimed: list[str | None] = []

    def claim(repository: JobRepository, runner: str) -> None:
        barrier.wait()
        result = repository.claim_next(runner, lease_seconds=30)
        claimed.append(result.id if result else None)

    threads = [
        Thread(target=claim, args=(first, "one")),
        Thread(target=claim, args=(second, "two")),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert claimed.count(job.id) == 1
    assert claimed.count(None) == 1


def test_recent_is_newest_first_and_bounded(tmp_path: Path) -> None:
    clock = Clock()
    repository = JobRepository(tmp_path / "jobs.sqlite3", clock=clock)
    ids = []
    for index in range(5):
        ids.append(repository.create(new_job(upload_name=f"{index}/source.pdf")).id)
        clock.advance(1)

    assert [job.id for job in repository.recent(limit=3)] == list(reversed(ids[-3:]))
    assert len(repository.recent(limit=1000)) == 5


def test_get_unknown_job_returns_none(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "jobs.sqlite3")

    assert repository.get("missing") is None


def test_failure_text_redacts_paths_and_credential_patterns(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "jobs.sqlite3")
    job = repository.create(new_job())
    repository.claim_next("runner", lease_seconds=30)

    failed = repository.fail(
        job.id,
        "cannot open /home/person/private/source.pdf API_KEY=top-secret Bearer abc123",
    )

    assert "/home/" not in failed.error
    assert "top-secret" not in failed.error
    assert "abc123" not in failed.error


def test_activity_is_persistent_and_not_advanced_by_heartbeat(tmp_path):
    clock = Clock()
    repo = JobRepository(tmp_path / 'jobs.sqlite3', clock=clock)
    job = repo.create(new_job())
    repo.claim_next('runner')
    repo.update(job.id, JobStage.PARSING, activity={'message': '正在识别版面'})
    activity = repo.get(job.id).activity
    clock.advance(10)
    repo.heartbeat(job.id, 'runner')
    assert repo.get(job.id).activity == activity
    clock.advance(10)
    repo.update(job.id, JobStage.REPAIRING, activity={'done': 1, 'total': 5, 'unit': '处'})
    current = JobRepository(repo.database).get(job.id)
    assert current.activity['done'] == 1
    assert current.activity['stage_started_at'] != activity['stage_started_at']
    repo.fail(job.id, 'test')
    repo.retry(job.id)
    assert repo.get(job.id).activity == {}


def test_existing_database_migrates_without_losing_jobs(tmp_path):
    import sqlite3
    path = tmp_path / 'jobs.sqlite3'
    repo = JobRepository(path)
    job = repo.create(new_job())
    with sqlite3.connect(path) as connection:
        connection.execute('ALTER TABLE jobs DROP COLUMN activity_json')
    migrated = JobRepository(path).get(job.id)
    assert migrated.original_filename == 'paper.pdf'
    assert migrated.activity == {}


def test_stage_events_and_recovery_budget(tmp_path):
    from datetime import datetime, timezone, timedelta
    from paper_library.jobs import JobRepository, NewJob, JobState, JobStage
    now=[datetime(2026,1,1,tzinfo=timezone.utc)]
    repo=JobRepository(tmp_path/'jobs.sqlite3',clock=lambda:now[0])
    job=repo.create(NewJob('paper.pdf','test','topic','en',True,True,'x/source.pdf'))
    for index in range(3):
        repo.claim_next('runner',lease_seconds=1)
        now[0]+=timedelta(seconds=2)
        repo.recover_expired()
    assert repo.get(job.id).state==JobState.FAILED
    assert len(repo.events(job.id))>=6
    repo.retry(job.id)
    assert repo.claim_next('runner').id==job.id


def test_queue_limit_and_cancelled_job_cannot_complete(tmp_path):
    import pytest
    from paper_library.jobs import JobRepository,NewJob,JobState
    from paper_library.jobs.models import InvalidTransition
    repo=JobRepository(tmp_path/'jobs.sqlite3',max_pending=1)
    request=NewJob('paper.pdf','lib','topic','en',True,True,'x/source.pdf')
    job=repo.create(request)
    with pytest.raises(ValueError):repo.create(request)
    repo.claim_next('runner');repo.request_cancel(job.id)
    with pytest.raises(InvalidTransition):repo.complete(job.id)
    repo.cancel(job.id)
    assert repo.get(job.id).state==JobState.CANCELLED
