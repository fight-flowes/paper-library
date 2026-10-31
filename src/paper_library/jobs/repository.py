"""SQLite persistence for import and translation jobs."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import json
import sqlite3
from typing import Any
from uuid import uuid4

from .models import (
    ALLOWED_TRANSITIONS,
    InvalidTransition,
    JobProgress,
    JobRecord,
    JobStage,
    JobState,
    NewJob,
    QueueFull,
)


Clock = Callable[[], datetime]
_UNSET = object()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_timestamp(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _safe_error(value: str) -> str:
    value = re.sub(r"(?i)\b(?:bearer|basic)\s+\S+", "凭据 [已隐藏]", value)
    value = re.sub(
        r'''(?i)(["']?\b(?:api[_-]?key|authorization|access[_-]?token|password|secret)\b["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,}]+)''',
        r'\1[已隐藏]', value,
    )
    sanitized = re.sub(
        r"(?i)\b(api[_-]?key|authorization)\b\s*[:=]\s*\S+",
        r"\1=[已隐藏]",
        value,
    )
    sanitized = re.sub(r"(?i)\bbearer\s+\S+", "Bearer [已隐藏]", sanitized)
    sanitized = re.sub(r"(?<![:\w])/(?:[^\s]+)", "[路径]", sanitized)
    return re.sub(r"\s+", " ", sanitized).strip()[:500] or "任务执行失败"


class JobRepository:
    """Durable job store with atomic queue claiming and leases."""

    def __init__(self, database: Path, *, clock: Clock = _utc_now, max_pending: int | None = None) -> None:
        self.database = database.expanduser().resolve()
        self.clock = clock
        self.max_pending = max_pending
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema (
                    version INTEGER NOT NULL
                );
                INSERT INTO schema(version)
                SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM schema);

                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    original_filename TEXT NOT NULL,
                    library_id TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    language TEXT NOT NULL,
                    enrich INTEGER NOT NULL,
                    auto_translate INTEGER NOT NULL,
                    upload_name TEXT NOT NULL,
                    state TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress_total INTEGER NOT NULL DEFAULT 0,
                    progress_cached INTEGER NOT NULL DEFAULT 0,
                    progress_translated INTEGER NOT NULL DEFAULT 0,
                    progress_failed INTEGER NOT NULL DEFAULT 0,
                    progress_pending INTEGER NOT NULL DEFAULT 0,
                    paper_id TEXT,
                    error TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    runner_id TEXT,
                    lease_expires_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS jobs_queue_idx
                    ON jobs(state, created_at);
                """
            )

            connection.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
            for column in ('activity_json', 'outcomes_json'):
                if column not in columns:
                    connection.execute(f"ALTER TABLE jobs ADD COLUMN {column} TEXT NOT NULL DEFAULT '{{}}'")

            if 'recovery_count' not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN recovery_count INTEGER NOT NULL DEFAULT 0")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS job_events (
                    id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, state TEXT NOT NULL,
                    stage TEXT NOT NULL, created_at TEXT NOT NULL, error TEXT
                );
                CREATE INDEX IF NOT EXISTS job_events_job ON job_events(job_id,id);
                CREATE TRIGGER IF NOT EXISTS job_stage_event AFTER UPDATE ON jobs
                WHEN OLD.state != NEW.state OR OLD.stage != NEW.stage
                BEGIN
                    INSERT INTO job_events(job_id,state,stage,created_at,error)
                    VALUES(NEW.id,NEW.state,NEW.stage,NEW.updated_at,NEW.error);
                END;
            """)

    def reconcile_metadata_notices(self, libraries) -> int:
        """Resolve stale metadata warnings only from a currently verified paper."""
        from paper_library.catalog import LibraryError, read_metadata
        updated = 0
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            rows = connection.execute(
                "SELECT * FROM jobs WHERE state = 'completed_with_warnings' AND paper_id IS NOT NULL"
            ).fetchall()
            for row in rows:
                try:
                    outcomes = json.loads(row['outcomes_json'])
                    if not isinstance(outcomes, dict) or outcomes.get('metadata', {}).get('status') != 'warning':
                        continue
                    snapshot = libraries.load(row['library_id'])
                    paper = snapshot.find(row['paper_id'])
                    if paper is None:
                        continue
                    metadata, _ = read_metadata(snapshot.descriptor.root, paper)
                    bibliography = metadata.get('bibliography') or {}
                    canonical = bibliography.get('canonical_title')
                    if (metadata.get('enrichment', {}).get('status') != 'verified'
                            or not isinstance(canonical, str)
                            or ' '.join(canonical.split()) != ' '.join(paper.title.split())):
                        continue
                except (LibraryError, OSError, ValueError, TypeError, AttributeError):
                    continue
                outcomes['metadata'] = {'status': 'completed', 'message': '论文信息已重新核验',
                                        'reason': 'verified', 'verified_at': _timestamp(self.clock())}
                warnings = (any(not isinstance(item, dict) or item.get('status') not in {'completed', 'skipped'}
                                for item in outcomes.values())
                            or row['progress_failed'] or row['progress_pending']
                            or row['progress_cached'] + row['progress_translated'] < row['progress_total'])
                state = JobState.COMPLETED_WITH_WARNINGS if warnings else JobState.COMPLETED
                connection.execute('UPDATE jobs SET state = ?, outcomes_json = ? WHERE id = ?',
                                   (state.value, json.dumps(outcomes, ensure_ascii=False), row['id']))
                updated += 1
        return updated

    def reconcile_quality_notices(self) -> int:
        """Correct historical jobs completed solely with readable translation notices."""
        updated = 0
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            rows = connection.execute(
                'SELECT id, outcomes_json, progress_total, progress_cached, '
                'progress_translated, progress_failed, progress_pending FROM jobs WHERE state = ?',
                (JobState.COMPLETED_WITH_WARNINGS.value,),
            ).fetchall()
            for row in rows:
                try:
                    outcomes = json.loads(row['outcomes_json'])
                except (TypeError, ValueError):
                    continue
                if not isinstance(outcomes, dict):
                    continue
                translation = outcomes.get('translation')
                if not isinstance(translation, dict) or translation.get('status') != 'warning':
                    continue
                if not isinstance(translation.get('reading_warnings'), int) or translation['reading_warnings'] <= 0:
                    continue
                if translation.get('failed') != 0:
                    continue
                if translation.get('integrity_warnings') != 0:
                    continue
                if row['progress_failed'] or row['progress_pending']:
                    continue
                if row['progress_cached'] + row['progress_translated'] < row['progress_total']:
                    continue
                if any(not isinstance(result, dict) or result.get('status') not in {'completed', 'skipped'}
                       for key, result in outcomes.items() if key != 'translation'):
                    continue
                outcomes['translation'] = {**translation, 'status': 'completed'}
                cursor = connection.execute(
                    'UPDATE jobs SET state = ?, outcomes_json = ? WHERE id = ? AND state = ?',
                    (JobState.COMPLETED.value, json.dumps(outcomes, ensure_ascii=False),
                     row['id'], JobState.COMPLETED_WITH_WARNINGS.value),
                )
                updated += cursor.rowcount
        return updated

    def events(self, job_id):
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT state,stage,created_at,error FROM job_events WHERE job_id=? ORDER BY id DESC LIMIT 200",(job_id,))]

    def release(self, job_id, runner_id):
        with self._connect() as connection:
            connection.execute("UPDATE jobs SET state='queued',runner_id=NULL,lease_expires_at=NULL,updated_at=? WHERE id=? AND state='running' AND runner_id=? AND cancel_requested=0",
                               (_timestamp(self.clock()),job_id,runner_id))

    def create(self, new_job: NewJob) -> JobRecord:
        job_id = uuid4().hex
        now = _timestamp(self.clock())
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            self._check_capacity(connection)
            connection.execute(
                """
                INSERT INTO jobs (
                    id, original_filename, library_id, subject, language,
                    enrich, auto_translate, upload_name, state, stage,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    new_job.original_filename,
                    new_job.library_id,
                    new_job.subject,
                    new_job.language,
                    int(new_job.enrich),
                    int(new_job.auto_translate),
                    new_job.upload_name,
                    JobState.QUEUED.value,
                    JobStage.QUEUED.value,
                    now,
                    now,
                ),
            )
        result = self.get(job_id)
        assert result is not None
        return result

    def get(self, job_id: str) -> JobRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._record(row) if row else None

    def recent(self, *, limit: int = 50) -> list[JobRecord]:
        bounded = max(1, min(int(limit), 200))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (bounded,),
            ).fetchall()
        return [self._record(row) for row in rows]

    def state_counts(self) -> dict[JobState, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT state, COUNT(*) AS count FROM jobs GROUP BY state"
            ).fetchall()
        return {JobState(row["state"]): int(row["count"]) for row in rows}

    def retryable_upload_names(self) -> set[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT upload_name FROM jobs WHERE state IN (?, ?, ?)",
                (
                    JobState.QUEUED.value,
                    JobState.RUNNING.value,
                    JobState.FAILED.value,
                ),
            ).fetchall()
        return {str(row["upload_name"]) for row in rows}

    def claim_next(self, runner_id: str, *, lease_seconds: int = 60) -> JobRecord | None:
        now_value = self.clock()
        now = _timestamp(now_value)
        lease = _timestamp(now_value + timedelta(seconds=max(1, lease_seconds)))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id FROM jobs WHERE state = ? ORDER BY created_at, rowid LIMIT 1",
                (JobState.QUEUED.value,),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                """
                UPDATE jobs
                SET state = ?, runner_id = ?, lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND state = ?
                """,
                (
                    JobState.RUNNING.value,
                    runner_id,
                    lease,
                    now,
                    row["id"],
                    JobState.QUEUED.value,
                ),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(row["id"])

    def heartbeat(self, job_id: str, runner_id: str, *, lease_seconds: int = 60) -> None:
        now_value = self.clock()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND state = ? AND runner_id = ?
                """,
                (
                    _timestamp(now_value + timedelta(seconds=max(1, lease_seconds))),
                    _timestamp(now_value),
                    job_id,
                    JobState.RUNNING.value,
                    runner_id,
                ),
            )
            if cursor.rowcount != 1:
                raise InvalidTransition("任务未由当前执行器运行")

    def update(
        self,
        job_id: str,
        stage: JobStage,
        progress: JobProgress | None = None,
        *,
        paper_id: str | None | object = _UNSET,
        activity: dict | None = None,
        outcomes: dict | None = None,
    ) -> JobRecord:
        job = self._required(job_id)
        if job.state is not JobState.RUNNING:
            raise InvalidTransition(f"不能更新 {job.state.value} 状态的任务")
        progress = progress or (JobProgress() if stage != job.stage else job.progress)
        now = _timestamp(self.clock())
        detail = dict(job.activity) if stage == job.stage else {'stage_started_at': now}
        detail.setdefault('stage_started_at', now)
        detail['phase'] = stage.value
        if stage != job.stage or progress != job.progress or activity is not None:
            detail['progressed_at'] = now
        if activity is not None:
            detail.update(activity)
        assignments: list[str] = [
            "stage = ?",
            "progress_total = ?",
            "progress_cached = ?",
            "progress_translated = ?",
            "progress_failed = ?",
            "progress_pending = ?",
            "updated_at = ?",
        ]
        values: list[Any] = [
            stage.value,
            max(0, progress.total),
            max(0, progress.cached),
            max(0, progress.translated),
            max(0, progress.failed),
            max(0, progress.pending),
            _timestamp(self.clock()),
        ]
        assignments.append("activity_json = ?")
        values.append(json.dumps(detail, ensure_ascii=False))
        if outcomes is not None:
            assignments.append('outcomes_json = ?')
            values.append(json.dumps(outcomes, ensure_ascii=False))
        if paper_id is not _UNSET:
            assignments.append("paper_id = ?")
            values.append(paper_id)
        values.append(job_id)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE jobs SET {', '.join(assignments)} WHERE id = ? AND state = 'running'",  # noqa: S608
                values,
            )
        return self._required(job_id)

    def request_cancel(self, job_id: str) -> JobRecord:
        job = self._required(job_id)
        now = _timestamp(self.clock())
        if job.state is JobState.RUNNING:
            with self._connect() as connection:
                connection.execute(
                    "UPDATE jobs SET cancel_requested = 1, updated_at = ? WHERE id = ? AND state = 'running'",
                    (now, job_id),
                )
        else:
            self._transition(job_id, JobState.CANCELLED, stage=JobStage.CANCELLED)
        return self._required(job_id)

    def cancel(self, job_id: str) -> JobRecord:
        """Finish a running job after its worker observes cancellation."""
        return self._transition(job_id, JobState.CANCELLED, stage=JobStage.CANCELLED)

    def cancel_requested(self, job_id: str) -> bool:
        return self._required(job_id).cancel_requested

    def retry(self, job_id: str) -> JobRecord:
        job = self._required(job_id)
        if job.state not in {JobState.FAILED, JobState.COMPLETED_WITH_WARNINGS}:
            raise InvalidTransition("只有失败或带警告完成的任务可以重试")
        self._assert_transition(job.state, JobState.QUEUED)
        now = _timestamp(self.clock())
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            self._check_capacity(connection)
            cursor = connection.execute(
                """
                UPDATE jobs SET state = ?, stage = ?, error = NULL,
                    cancel_requested = 0, runner_id = NULL, lease_expires_at = NULL,
                    progress_total = 0, progress_cached = 0,
                    progress_translated = 0, progress_failed = 0,
                    progress_pending = 0, recovery_count = 0, activity_json = '{}', updated_at = ?
                WHERE id = ? AND state = ?
                """,
                (JobState.QUEUED.value, JobStage.QUEUED.value, now, job_id, job.state.value),
            )
            if cursor.rowcount != 1:
                raise InvalidTransition("任务状态已变化，请刷新后重试")
        return self._required(job_id)

    def enable_translation(self, job_id: str) -> JobRecord:
        job = self._required(job_id)
        if job.state not in {JobState.COMPLETED, JobState.COMPLETED_WITH_WARNINGS} or not job.paper_id:
            raise InvalidTransition("只有已完成解析的任务可以继续翻译")
        self._assert_transition(job.state, JobState.QUEUED)
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            self._check_capacity(connection)
            cursor = connection.execute(
                """
                UPDATE jobs SET auto_translate = 1, state = ?, stage = ?,
                    error = NULL, cancel_requested = 0, runner_id = NULL,
                    activity_json = '{}', lease_expires_at = NULL, updated_at = ? WHERE id = ? AND state = ?
                """,
                (
                    JobState.QUEUED.value,
                    JobStage.PUBLISHED.value,
                    _timestamp(self.clock()),
                    job_id,
                    job.state.value,
                ),
            )
            if cursor.rowcount != 1:
                raise InvalidTransition("任务状态已变化，请刷新后重试")
        return self._required(job_id)

    def _check_capacity(self, connection):
        if self.max_pending is not None and connection.execute(
                "SELECT count(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0] >= self.max_pending:
            raise QueueFull('待处理任务已达上限，请等待现有任务完成后重试')

    def complete(self, job_id: str, *, warnings: bool = False) -> JobRecord:
        target = JobState.COMPLETED_WITH_WARNINGS if warnings else JobState.COMPLETED
        return self._transition(job_id, target, stage=JobStage.COMPLETED)

    def fail(self, job_id: str, error: str) -> JobRecord:
        return self._transition(
            job_id,
            JobState.FAILED,
            stage=JobStage.FAILED,
            error=_safe_error(error),
        )

    def recover_expired(self, *, max_recoveries: int = 3) -> int:
        now = _timestamp(self.clock())
        with self._connect() as connection:
            cancelled = connection.execute(
                """
                UPDATE jobs SET state = ?, stage = ?, runner_id = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE state = ? AND cancel_requested = 1
                    AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?
                """,
                (
                    JobState.CANCELLED.value,
                    JobStage.CANCELLED.value,
                    now,
                    JobState.RUNNING.value,
                    now,
                ),
            ).rowcount
            queued = connection.execute(
                """
                UPDATE jobs SET state = CASE WHEN recovery_count + 1 >= ? THEN 'failed' ELSE 'queued' END,
                    error = CASE WHEN recovery_count + 1 >= ? THEN '自动恢复次数已达上限，请检查日志后手动重试' ELSE error END,
                    recovery_count = recovery_count + 1, runner_id = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE state = ? AND cancel_requested = 0
                    AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?
                """,
                (max_recoveries, max_recoveries, now, JobState.RUNNING.value, now),
            ).rowcount
        return cancelled + queued

    def _transition(
        self,
        job_id: str,
        target: JobState,
        *,
        stage: JobStage,
        error: str | None = None,
    ) -> JobRecord:
        job = self._required(job_id)
        self._assert_transition(job.state, target)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET state = ?, stage = ?, error = ?, runner_id = NULL,
                    lease_expires_at = NULL, updated_at = ? WHERE id = ? AND state = ?
                    AND (cancel_requested = 0 OR ? = 'cancelled')
                """,
                (target.value, stage.value, error, _timestamp(self.clock()), job_id, job.state.value, target.value),
            )
            if cursor.rowcount != 1:
                raise InvalidTransition("任务状态已变化，请重新读取后操作")
        return self._required(job_id)

    @staticmethod
    def _assert_transition(source: JobState, target: JobState) -> None:
        if target not in ALLOWED_TRANSITIONS[source]:
            raise InvalidTransition(f"无效任务状态转换：{source.value} -> {target.value}")

    def _required(self, job_id: str) -> JobRecord:
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    @staticmethod
    def _record(row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            id=row["id"],
            original_filename=row["original_filename"],
            library_id=row["library_id"],
            subject=row["subject"],
            language=row["language"],
            enrich=bool(row["enrich"]),
            auto_translate=bool(row["auto_translate"]),
            upload_name=row["upload_name"],
            state=JobState(row["state"]),
            stage=JobStage(row["stage"]),
            progress=JobProgress(
                total=row["progress_total"],
                cached=row["progress_cached"],
                translated=row["progress_translated"],
                failed=row["progress_failed"],
                pending=row["progress_pending"],
            ),
            activity=json.loads(row["activity_json"]),
            outcomes=json.loads(row["outcomes_json"]),
            paper_id=row["paper_id"],
            error=row["error"],
            cancel_requested=bool(row["cancel_requested"]),
            runner_id=row["runner_id"],
            lease_expires_at=_parse_timestamp(row["lease_expires_at"]),
            created_at=_parse_timestamp(row["created_at"]),
            updated_at=_parse_timestamp(row["updated_at"]),
        )
