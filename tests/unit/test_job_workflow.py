from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
import json
import hashlib

from paper_library.catalog import LibraryRepository, PaperPaths
from paper_library.ingest.service import IngestOutcome, IngestResult
from paper_library.jobs import JobRepository, JobStage, JobState, NewJob
from paper_library.jobs.workflow import JobWorkflow


class Uploads:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.removed: list[str] = []

    def resolve(self, name: str) -> Path:
        return self.root / name

    def remove(self, name: str) -> None:
        self.removed.append(name)
        (self.root / name).unlink(missing_ok=True)


@dataclass
class Context:
    jobs: JobRepository
    uploads: Uploads
    libraries: LibraryRepository
    workflow: JobWorkflow
    upload_name: str
    ingest_calls: list[str]
    batch_calls: list[str]

    def create(self, *, auto_translate: bool = True):
        upload = self.uploads.root / self.upload_name
        upload.parent.mkdir(parents=True, exist_ok=True)
        upload.write_bytes(b"%PDF-job")
        queued = self.jobs.create(
            NewJob("paper.pdf", "whitespace", "topic", "en", False,
                   auto_translate, self.upload_name)
        )
        return self.jobs.claim_next("runner", lease_seconds=30)


@pytest.fixture
def job_context(tmp_path: Path) -> Context:
    library = tmp_path / "libraries" / "whitespace"
    library.mkdir(parents=True)
    (library / "library.toml").write_text(
        'schema_version = 1\nid = "whitespace"\nname = "Whitespace"\n'
        'paper_id_prefix = "ws"\n', encoding="utf-8"
    )
    (library / "catalog.tsv").write_text(
        "id\tyear\tvenue\tshort_name\tsubject\ttitle\tsha256\tparsed_at\tpublication_date\taliases\n",
        encoding="utf-8",
    )
    paths = PaperPaths.for_id(library, 'ws-p0003')
    paths.root.mkdir(parents=True)
    paths.markdown.write_text('# Fixture\n\nBody text.')
    paths.metadata.write_text(json.dumps({'enrichment': {'status': 'verified'}}))
    reports = paths.root / 'paragraph-repairs' / 'fixture'
    reports.mkdir(parents=True)
    (reports / 'report.json').write_text(json.dumps({'enabled': True, 'merged': 0, 'decisions': [],
        'output_sha256': hashlib.sha256(paths.markdown.read_bytes()).hexdigest()}))
    jobs = JobRepository(tmp_path / "jobs.sqlite3")
    uploads = Uploads(tmp_path / "uploads")
    libraries = LibraryRepository(tmp_path / "libraries")
    ingest_calls: list[str] = []
    batch_calls: list[str] = []

    def ingest(repository, library_id, source, **kwargs):
        ingest_calls.append(library_id)
        paths = PaperPaths.for_id(library, "ws-p0003")
        return IngestResult(library_id, "ws-p0003", IngestOutcome.CREATED, paths)

    def load(_repository, library_id, paper_id):
        assert library_id == "whitespace"
        assert paper_id == "ws-p0003"
        return ["one", "two"]

    def batch(tasks, service, **kwargs):
        batch_calls.append("run")
        report = {"total": 2, "cached": 1, "translated": 1, "failed": 0, "pending": 0}
        kwargs["on_update"](report, None)
        return {**report, "interrupted": False}

    service = SimpleNamespace(config=SimpleNamespace(concurrency=16))
    workflow = JobWorkflow(
        jobs,
        uploads,
        libraries,
        service,
        ingest=ingest,
        task_loader=load,
        batch_runner=batch,
    )
    return Context(jobs, uploads, libraries, workflow, "abc/source.pdf", ingest_calls, batch_calls)


def test_parse_only_job_publishes_and_completes(job_context: Context) -> None:
    job = job_context.create(auto_translate=False)
    assert job is not None

    job_context.workflow.run(job, cancelled=lambda: False)

    saved = job_context.jobs.get(job.id)
    assert saved.state is JobState.COMPLETED
    assert saved.stage is JobStage.COMPLETED
    assert saved.paper_id == "ws-p0003"
    assert job_context.batch_calls == []
    assert not (job_context.uploads.root / job_context.upload_name).exists()


def test_translation_progress_and_warning_completion(job_context: Context) -> None:
    job = job_context.create()
    assert job is not None

    def warning_batch(tasks, service, **kwargs):
        report = {"total": 3, "cached": 1, "translated": 1, "failed": 1, "pending": 0}
        kwargs["on_update"](report, "one warning")
        return {**report, "interrupted": False}

    job_context.workflow.batch_runner = warning_batch
    job_context.workflow.run(job, cancelled=lambda: False)

    saved = job_context.jobs.get(job.id)
    assert saved.state is JobState.COMPLETED_WITH_WARNINGS
    assert saved.progress.cached == 1
    assert saved.progress.failed == 1


def test_published_job_resumes_translation_without_ingest(job_context: Context) -> None:
    job = job_context.create()
    assert job is not None
    job_context.jobs.update(job.id, JobStage.PUBLISHED, paper_id="ws-p0003")
    resumed = job_context.jobs.get(job.id)

    job_context.workflow.run(resumed, cancelled=lambda: False)

    assert job_context.ingest_calls == []
    assert job_context.batch_calls == ["run"]
    assert job_context.jobs.get(job.id).state is JobState.COMPLETED


def test_model_failure_can_retry_without_rerunning_ingest(job_context: Context) -> None:
    job = job_context.create()
    assert job is not None
    attempts = 0

    def failing_batch(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("model offline")
        return {"total": 2, "cached": 0, "translated": 2, "failed": 0,
                "pending": 0, "interrupted": False}

    job_context.workflow.batch_runner = failing_batch
    with pytest.raises(RuntimeError, match="offline"):
        job_context.workflow.run(job, cancelled=lambda: False)
    job_context.jobs.fail(job.id, "translation unavailable")
    job_context.jobs.retry(job.id)
    retried = job_context.jobs.claim_next("runner", lease_seconds=30)

    job_context.workflow.run(retried, cancelled=lambda: False)

    assert job_context.ingest_calls == ["whitespace"]
    assert attempts == 2
    assert job_context.jobs.get(job.id).state is JobState.COMPLETED


def test_cancelled_workflow_marks_job_and_removes_upload(job_context: Context) -> None:
    job = job_context.create()
    assert job is not None

    job_context.workflow.run(job, cancelled=lambda: True)

    assert job_context.jobs.get(job.id).state is JobState.CANCELLED
    assert job_context.uploads.removed == [job_context.upload_name]


def test_persisted_cancel_is_observed_without_process_event(job_context: Context) -> None:
    job = job_context.create()
    assert job is not None
    job_context.jobs.request_cancel(job.id)

    job_context.workflow.run(job, cancelled=lambda: False)

    assert job_context.jobs.get(job.id).state is JobState.CANCELLED
    assert job_context.ingest_calls == []


def test_all_translation_segments_failed_still_generates_guide(job_context: Context) -> None:
    job = job_context.create()
    assert job is not None

    def failed_batch(tasks, service, **kwargs):
        return {"total": 2, "cached": 0, "translated": 0, "failed": 2,
                "pending": 0, "interrupted": False}

    guided = []
    job_context.workflow.guide_generator = lambda *a, **kw: guided.append(True)
    job_context.workflow.batch_runner = failed_batch
    job_context.workflow.run(job, cancelled=lambda: False)

    saved = job_context.jobs.get(job.id)
    assert saved.state is JobState.COMPLETED_WITH_WARNINGS
    assert saved.paper_id == "ws-p0003"
    assert guided == [True]
    assert not (job_context.uploads.root / job_context.upload_name).exists()


def test_ingest_stage_events_reach_durable_job(job_context):
    context = job_context
    job = context.create(auto_translate=False)
    original = context.workflow.ingest
    seen = []
    def ingest(*args, **kwargs):
        kwargs['on_event']('loading secret/path and tokens should not be displayed')
        seen.append(context.jobs.get(job.id))
        kwargs['on_progress']('repairing', {'message': '检查断点', 'done': 2, 'total': 3, 'unit': '处'})
        seen.append(context.jobs.get(job.id))
        kwargs['on_progress']('enriching', {'message': '核对信息'})
        seen.append(context.jobs.get(job.id))
        return original(*args, **kwargs)
    context.workflow.ingest = ingest
    context.workflow.run(job, lambda: False)
    assert seen[0].activity['message'] == '正在加载解析模型'
    assert seen[1].stage == JobStage.REPAIRING
    assert seen[1].activity['done'] == 2
    assert seen[2].stage == JobStage.ENRICHING
    assert 'total' not in seen[2].activity


def test_readable_translation_notices_do_not_block_completion(job_context):
    context = job_context
    job = context.create()
    called = []
    context.workflow.guide_generator = lambda path, **kwargs: called.append(path)
    def batch(*args, **kwargs):
        return {'total': 2, 'cached': 2, 'translated': 0, 'failed': 0, 'pending': 0,
                'results': [{'warnings': ['missing formula']}, {'warnings': []}]}
    context.workflow.batch_runner = batch
    context.workflow.run(job, lambda: False)
    saved = context.jobs.get(job.id)
    assert saved.state == JobState.COMPLETED
    assert saved.outcomes['translation']['status'] == 'completed'
    assert saved.outcomes['translation']['reading_warnings'] == 1
    assert saved.outcomes['guide']['status'] == 'completed'
    assert len(called) == 1
    assert saved.progress.total == 2


def test_translation_content_loss_remains_a_warning_after_requests_complete(job_context):
    context = job_context
    job = context.create()
    context.workflow.batch_runner = lambda *a, **kw: {
        'total': 2, 'cached': 2, 'translated': 0, 'failed': 0, 'pending': 0,
        'results': [{'warnings': ['missing marker'], 'integrity_warnings': ['missing marker']},
                    {'warnings': [], 'integrity_warnings': []}]}
    context.workflow.run(job, lambda: False)
    saved = context.jobs.get(job.id)
    assert saved.state == JobState.COMPLETED_WITH_WARNINGS
    assert saved.outcomes['translation']['status'] == 'warning'
    assert saved.outcomes['translation']['integrity_warnings'] == 1
    assert context.jobs.reconcile_quality_notices() == 0


def test_reused_paper_only_retries_missing_or_incomplete_translation(job_context):
    context = job_context
    job = context.create()
    context.jobs.update(job.id, JobStage.PUBLISHED, paper_id='ws-p0003')
    seen = []

    def batch(*args, **kwargs):
        seen.append((kwargs['retry_warnings'], kwargs['retry_integrity']))
        return {'total': 2, 'cached': 2, 'translated': 0, 'failed': 0,
                'pending': 0, 'interrupted': False}

    context.workflow.batch_runner = batch
    context.workflow.run(context.jobs.get(job.id), lambda: False)
    assert seen == [(False, True)]


def test_guide_failure_is_visible_and_retry_does_not_parse_again(job_context):
    context = job_context
    job = context.create()
    def fail(*args, **kwargs):
        raise ValueError('quote mismatch')
    context.workflow.guide_generator = fail
    context.workflow.run(job, lambda: False)
    saved = context.jobs.get(job.id)
    assert saved.state == JobState.COMPLETED_WITH_WARNINGS
    assert saved.outcomes['guide']['status'] == 'warning'
    context.jobs.retry(job.id)
    context.workflow.guide_generator = lambda *args, **kwargs: None
    context.workflow.run(context.jobs.claim_next('runner'), lambda: False)
    assert context.ingest_calls == ['whitespace']
    assert context.jobs.get(job.id).state == JobState.COMPLETED


def test_unverified_metadata_is_a_warning_even_with_successful_translation(job_context):
    from dataclasses import replace
    context = job_context
    job = replace(context.create(), enrich=True)
    paths = PaperPaths.for_id(context.libraries.load('whitespace').descriptor.root, 'ws-p0003')
    paths.metadata.write_text(json.dumps({'enrichment': {'status': 'ambiguous'}}))
    context.workflow.run(job, lambda: False)
    saved = context.jobs.get(job.id)
    assert saved.state == JobState.COMPLETED_WITH_WARNINGS
    assert saved.outcomes['metadata']['status'] == 'warning'
    assert saved.outcomes['translation']['status'] == 'completed'


@pytest.mark.parametrize(('reason', 'message'), [
    ('unavailable', '检索服务暂不可用'),
    ('not_found', '未找到可核实的官方记录'),
    ('ambiguous', '找到候选，但证据不足'),
    ('conflict', '官方记录与本地论文信息冲突'),
])
def test_metadata_warning_explains_reason_and_safe_candidates(job_context, reason, message):
    from dataclasses import replace
    context = job_context
    job = replace(context.create(auto_translate=False), enrich=True)
    paths = PaperPaths.for_id(context.libraries.load('whitespace').descriptor.root, 'ws-p0003')
    paths.metadata.write_text(json.dumps({'enrichment': {'status': reason, 'candidates': [
        {'title': 'Official candidate', 'url': 'https://arxiv.org/abs/2510.10271', 'domain': 'arxiv.org'},
        {'title': 'Unsafe', 'url': 'http://internal.invalid/paper', 'domain': 'internal.invalid'},
    ]}}))
    context.workflow.run(job, lambda: False)
    outcome = context.jobs.get(job.id).outcomes['metadata']
    assert outcome['status'] == 'warning'
    assert message in outcome['message']
    assert outcome['reason'] == reason
    assert outcome['candidates'] == [
        {'title': 'Official candidate', 'url': 'https://arxiv.org/abs/2510.10271', 'domain': 'arxiv.org'}]


def test_structure_stage_runs_before_translation_with_progress(job_context):
    context = job_context
    job = context.create()
    def structure(path, **kwargs):
        assert context.batch_calls == []
        kwargs['progress'](3,5)
        saved = context.jobs.get(job.id)
        assert saved.stage == JobStage.STRUCTURING
        assert saved.activity['done'] == 3
        return {'annotations':[{'id':'source-1','role':'abstract'}]}
    context.workflow.structure_generator = structure
    context.workflow.run(job, lambda:False)
    assert context.jobs.get(job.id).outcomes['structure']['status'] == 'completed'
    assert context.batch_calls == ['run']


def test_structure_failure_does_not_block_translation(job_context):
    context = job_context
    job = context.create()
    def structure(*args, **kwargs):
        raise ValueError('Invalid annotation')
    context.workflow.structure_generator = structure
    context.workflow.run(job, lambda:False)
    saved = context.jobs.get(job.id)
    assert saved.state == JobState.COMPLETED_WITH_WARNINGS
    assert saved.outcomes['structure']['status'] == 'warning'
    assert context.batch_calls == ['run']


def test_duplicate_import_checks_missing_metadata_and_repairs(job_context, monkeypatch):
    from dataclasses import replace
    context = job_context
    job = replace(context.create(), enrich=True)
    paths = PaperPaths.for_id(context.libraries.load('whitespace').descriptor.root, 'ws-p0003')
    paths.metadata.write_text('{"enrichment":{"status":"unavailable"}}')
    for path in paths.root.glob('paragraph-repairs/*/report.json'): path.unlink()
    context.workflow.ingest = lambda *a, **kw: IngestResult('whitespace','ws-p0003',IngestOutcome.UNCHANGED,paths)
    calls = []
    def refresh(*a, **kw):
        calls.append('metadata')
        paths.metadata.write_text('{"enrichment":{"status":"verified"}}')
    monkeypatch.setattr('paper_library.jobs.workflow.refresh_metadata',refresh)
    def repair(*a, **kw):
        calls.append('repair')
        return {'enabled':True,'merged':0,'decisions':[]}
    context.workflow.paragraph_repairer = repair
    context.workflow.run(job,lambda:False)
    assert calls == ['metadata','repair']


def test_warning_job_can_enable_translation(job_context):
    context = job_context; job = context.create(auto_translate=False)
    context.workflow.structure_generator = lambda *a, **kw: {'annotations':[{'id':'source-1','role':'unknown'}]}
    context.workflow.run(job,lambda:False)
    queued = context.jobs.enable_translation(job.id)
    assert queued.auto_translate and queued.state == JobState.QUEUED
    context.workflow.run(context.jobs.claim_next('runner'),lambda:False)
    assert context.ingest_calls == ['whitespace']
    assert context.batch_calls == ['run']


def test_translation_service_error_still_runs_guide(job_context):
    from paper_library.translation.service import TranslationError
    context = job_context; job=context.create(); guided=[]
    def fail(*a,**kw): raise TranslationError('Model not configured')
    context.workflow.batch_runner=fail
    context.workflow.guide_generator=lambda *a,**kw:guided.append(True)
    context.workflow.run(job,lambda:False)
    assert guided == [True]
    saved=context.jobs.get(job.id)
    assert saved.state == JobState.COMPLETED_WITH_WARNINGS
    assert saved.outcomes['translation']['status'] == 'warning'
