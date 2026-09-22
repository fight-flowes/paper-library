"""Immutable records used by the durable background-job subsystem."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobStage(str, Enum):
    QUEUED = "queued"
    PARSING = "parsing"
    REPAIRING = "repairing"
    ENRICHING = "enriching"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    STRUCTURING = "structuring"
    TRANSLATING = "translating"
    GUIDING = "guiding"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


ALLOWED_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.RUNNING, JobState.CANCELLED}),
    JobState.RUNNING: frozenset(
        {
            JobState.QUEUED,
            JobState.COMPLETED,
            JobState.COMPLETED_WITH_WARNINGS,
            JobState.FAILED,
            JobState.CANCELLED,
        }
    ),
    JobState.FAILED: frozenset({JobState.QUEUED, JobState.CANCELLED}),
    JobState.COMPLETED: frozenset({JobState.QUEUED}),
    JobState.COMPLETED_WITH_WARNINGS: frozenset({JobState.QUEUED}),
    JobState.CANCELLED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class JobProgress:
    total: int = 0
    cached: int = 0
    translated: int = 0
    failed: int = 0
    pending: int = 0


@dataclass(frozen=True, slots=True)
class NewJob:
    original_filename: str
    library_id: str
    subject: str
    language: str
    enrich: bool
    auto_translate: bool
    upload_name: str


@dataclass(frozen=True, slots=True)
class JobRecord:
    id: str
    original_filename: str
    library_id: str
    subject: str
    language: str
    enrich: bool
    auto_translate: bool
    upload_name: str
    state: JobState = JobState.QUEUED
    stage: JobStage = JobStage.QUEUED
    progress: JobProgress = field(default_factory=JobProgress)
    activity: dict = field(default_factory=dict)
    outcomes: dict = field(default_factory=dict)
    paper_id: str | None = None
    error: str | None = None
    cancel_requested: bool = False
    runner_id: str | None = None
    lease_expires_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class InvalidTransition(RuntimeError):
    """Raised when a durable job is moved to an invalid state."""
