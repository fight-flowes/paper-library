"""Phase-aware orchestration for one imported paper."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import time
import json
import hashlib
from typing import Any

from paper_library.catalog import LibraryRepository, PaperPaths
from paper_library.ingest.service import ingest_pdf, refresh_metadata, IngestOutcome
from paper_library.translation.batch import load_tasks, run_batch
from paper_library.translation.service import TranslationService, TranslationError

from .models import JobProgress, JobRecord, JobStage
from .repository import JobRepository, _safe_error
from .uploads import UploadStager
from paper_library.storage.locking import gate, paper_lock
from paper_library.storage.history import checkpoint


class _CancellationView:
    def __init__(self, cancelled: Callable[[], bool]) -> None:
        self._cancelled = cancelled

    def is_set(self) -> bool:
        return self._cancelled()

    def wait(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while not self._cancelled():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.05, remaining))
        return True


class JobWorkflow:
    def __init__(
        self,
        jobs: JobRepository,
        uploads: UploadStager,
        libraries: LibraryRepository,
        translation_service: TranslationService,
        *,
        ingest: Callable[..., Any] = ingest_pdf,
        task_loader: Callable[..., list[Any]] = load_tasks,
        batch_runner: Callable[..., dict[str, Any]] = run_batch,
        retries: int = 2,
        guide_generator=None,
        paragraph_repairer=None,
        structure_generator=None,
        project_root=None,
    ) -> None:
        self.jobs = jobs
        self.uploads = uploads
        self.libraries = libraries
        self.translation_service = translation_service
        self.ingest = ingest
        self.task_loader = task_loader
        self.batch_runner = batch_runner
        self.retries = retries
        self.guide_generator = guide_generator
        self.paragraph_repairer = paragraph_repairer
        self.structure_generator = structure_generator
        self.project_root = project_root or libraries.root.parent

    def run(self, job: JobRecord, cancelled: Callable[[], bool]) -> None:
        def observed_cancel() -> bool:
            return cancelled() or self.jobs.cancel_requested(job.id)

        try:
            with gate(self.project_root):
                self._run(job, observed_cancel)
        except BaseException:
            if observed_cancel():
                current = self.jobs.get(job.id)
                if current is not None and current.state.value == "running":
                    self.jobs.cancel(job.id)
                self.uploads.remove(job.upload_name)
                return
            current = self.jobs.get(job.id)
            if current and current.state.value == 'running':
                for key, result in current.outcomes.items():
                    if result.get('status') == 'running':
                        self._outcome(job, key, 'warning', '该阶段未完成，可重试')
            raise

    def _run(self, job: JobRecord, cancelled: Callable[[], bool]) -> None:
        if cancelled():
            self.jobs.cancel(job.id)
            self.uploads.remove(job.upload_name)
            return

        reused_paper = job.paper_id is not None
        if job.paper_id is None:
            self.jobs.update(job.id, JobStage.PARSING)
            def parser_event(message: str) -> None:
                # Only expose recognized processing steps, never raw parser logs.
                text = message.lower()
                labels = [('ocr', '正在识别文字'), ('layout', '正在分析版面'),
                          ('formula', '正在识别公式'), ('table', '正在识别表格'),
                          ('predict', '正在解析页面内容'), ('loading', '正在加载解析模型')]
                for word, label in labels:
                    if word in text:
                        self.jobs.update(job.id, JobStage.PARSING, activity={'message': label})
                        break

            result = self.ingest(
                self.libraries,
                job.library_id,
                self.uploads.resolve(job.upload_name),
                subject=job.subject,
                language=job.language,
                enrich=job.enrich,
                cancelled=cancelled,
                original_filename=job.original_filename,
                on_event=parser_event,
                on_progress=lambda stage, detail: self.jobs.update(job.id, JobStage(stage), activity=detail),
            )
            self.jobs.update(job.id, JobStage.PUBLISHED, paper_id=result.paper_id)
            paper_id = result.paper_id
            reused_paper = result.outcome == IngestOutcome.UNCHANGED
        else:
            paper_id = job.paper_id

        if cancelled():
            self.jobs.cancel(job.id)
            self.uploads.remove(job.upload_name)
            return
        paths = PaperPaths.for_id(self.libraries.load(job.library_id).descriptor.root, paper_id)
        with paper_lock(paths.root):
            checkpoint(paths.root, 'before import processing')
            try:
                self._process_published(job, paper_id, cancelled, reused_paper)
            finally:
                checkpoint(paths.root, 'after import processing')

    def _process_published(self, job, paper_id, cancelled, reused_paper):
        self._check_import_results(job, paper_id, cancelled, recheck=reused_paper)
        if self.structure_generator is not None and not cancelled():
            paths = PaperPaths.for_id(self.libraries.load(job.library_id).descriptor.root, paper_id)
            self.jobs.update(job.id, JobStage.STRUCTURING, activity={'message': '正在校对文本结构'})
            self._outcome(job, 'structure', 'running', '正在校对文本结构')
            try:
                result = self.structure_generator(paths.markdown, cancelled=cancelled,
                    progress=lambda done, total: self.jobs.update(job.id, JobStage.STRUCTURING,
                        activity={'message': '正在校对文本结构', 'done': done, 'total': total, 'unit': '块'}))
                uncertain = sum(row['role'] == 'unknown' for row in result['annotations'])
                self._outcome(job, 'structure', 'warning' if uncertain else 'completed',
                    f"已校对 {len(result['annotations'])} 块；保留原样待确认 {uncertain} 块")
            except (ValueError, OSError) as exc:
                self._outcome(job, 'structure', 'warning', '结构校对未完成，保留原排版：' + _safe_error(str(exc)))
        if cancelled():
            self.jobs.cancel(job.id)
            self.uploads.remove(job.upload_name)
            return
        if not job.auto_translate:
            self._outcome(job, 'translation', 'skipped', '未启用自动翻译')
            self._finish(job, paper_id, cancelled)
            self.uploads.remove(job.upload_name)
            return

        tasks = self.task_loader(self.libraries, job.library_id, paper_id)
        self.jobs.update(
            job.id,
            JobStage.TRANSLATING,
            JobProgress(total=len(tasks), pending=len(tasks)),
        )

        self._outcome(job, 'translation', 'running', '正在处理段落译文')

        def update_progress(report: dict[str, Any], _message: str | None) -> None:
            total = int(report.get("total", len(tasks)))
            cached = int(report.get("cached", 0))
            translated = int(report.get("translated", 0))
            failed = int(report.get("failed", 0))
            pending = int(report.get("pending", max(0, total - cached - translated - failed)))
            self.jobs.update(
                job.id,
                JobStage.TRANSLATING,
                JobProgress(total, cached, translated, failed, pending),
            )

        concurrency = self.translation_service.config.concurrency
        service_failed = False
        try:
            report = self.batch_runner(
                tasks,
                self.translation_service,
                concurrency=concurrency,
                retries=self.retries,
                stop_event=_CancellationView(cancelled),
                progress=lambda _message: None,
                on_update=update_progress,
                retry_warnings=reused_paper,
            )
        except TranslationError:
            # Service/configuration failures are recoverable per-stage failures.
            # Programming errors still propagate to the job runner.
            service_failed = True
            saved = self.jobs.get(job.id).progress
            report = dict(total=len(tasks), cached=saved.cached, translated=saved.translated,
                          failed=max(0, len(tasks) - saved.cached - saved.translated),
                          pending=0, results=[], interrupted=False)
        update_progress(report, None)
        reading_warnings = sum(bool(item.get('warnings')) for item in report.get('results', []))
        failed = int(report.get('failed', 0))
        self._outcome(job, 'translation', 'warning' if failed or reading_warnings or service_failed else 'completed',
                      f"已保存 {report.get('cached', 0) + report.get('translated', 0)} / {report.get('total', 0)} 段；失败 {failed} 段；阅读提示 {reading_warnings} 段",
                      failed=failed, reading_warnings=reading_warnings)
        if cancelled() or report.get("interrupted"):
            self.jobs.cancel(job.id)
        else:
            self._finish(job, paper_id, cancelled)
        self.uploads.remove(job.upload_name)

    def _outcome(self, job, key, status, message, **details):
        current = self.jobs.get(job.id)
        outcomes = {**current.outcomes, key: dict(status=status, message=message, **details)}
        self.jobs.update(job.id, current.stage, current.progress, outcomes=outcomes)

    def _check_import_results(self, job, paper_id, cancelled, *, recheck=False):
        paths = PaperPaths.for_id(self.libraries.load(job.library_id).descriptor.root, paper_id)
        self._outcome(job, 'parsing', 'completed', 'PDF 已解析并入库')
        try:
            metadata = json.loads(paths.metadata.read_text())
        except (OSError, ValueError):
            metadata = {}
        if recheck and not cancelled() and job.enrich and metadata.get('enrichment', {}).get('status') != 'verified':
            self.jobs.update(job.id, JobStage.ENRICHING, activity={'message': '重新检索并核对论文信息'})
            try:
                refresh_metadata(self.libraries, job.library_id, paper_id)
                metadata = json.loads(paths.metadata.read_text())
            except (ValueError, OSError):
                pass
        enrichment = metadata.get('enrichment', {})
        status = enrichment.get('status')
        self._outcome(job, 'metadata', 'completed' if status == 'verified' else 'warning' if job.enrich else 'skipped',
                      '论文信息已核验' if status == 'verified' else '论文信息未能确认，可重试核验' if job.enrich else '未启用信息检索')
        source_hash = hashlib.sha256(paths.markdown.read_bytes()).hexdigest() if paths.markdown.exists() else None
        reports = []
        for path in paths.root.glob('paragraph-repairs/*/report.json'):
            try:
                report = json.loads(path.read_text())
                if report.get('output_sha256') == source_hash:
                    reports.append((path.stat().st_mtime, report))
            except (ValueError, OSError):
                continue
        latest = max(reports, key=lambda item: item[0])[1] if reports else None
        needs_review = latest is None or any(item.get('decision') == 'uncertain' for item in latest.get('decisions', []))
        if recheck and needs_review and self.paragraph_repairer and not cancelled():
            self.jobs.update(job.id, JobStage.REPAIRING, activity={'message': '重新检查段落连续性'})
            try:
                latest = self.paragraph_repairer(paths.markdown, cancelled=cancelled,
                    on_progress=lambda done, total: self.jobs.update(job.id, JobStage.REPAIRING,
                        activity={'message': '重新检查段落连续性', 'done': done, 'total': total, 'unit': '处'}))
            except (ValueError, OSError):
                latest = None
        if latest is not None:
            report = latest
            uncertain = sum(item.get('decision') == 'uncertain' for item in report.get('decisions', []))
            self._outcome(job, 'repairing', 'warning' if uncertain else 'completed' if report.get('enabled') else 'skipped',
                          f"检查 {len(report.get('decisions', []))} 处，合并 {report.get('merged', 0)} 处，待确认 {uncertain} 处")
        else:
            self._outcome(job, 'repairing', 'warning', '缺少与当前正文对应的段落检查记录')

    def _finish(self, job, paper_id, cancelled):
        if cancelled():
            self.jobs.cancel(job.id)
            return
        if self.guide_generator is not None:
            paths = PaperPaths.for_id(self.libraries.load(job.library_id).descriptor.root, paper_id)
            current = self.jobs.get(job.id)
            self.jobs.update(job.id, JobStage.GUIDING, current.progress, activity={'message': '正在生成论文导读'})
            self._outcome(job, 'guide', 'running', '正在生成导读')
            try:
                self.guide_generator(paths.markdown, cancelled=cancelled,
                    progress=lambda message: self.jobs.update(job.id, JobStage.GUIDING, current.progress,
                                                              activity={'message': message}))
                self._outcome(job, 'guide', 'completed', '导读已生成')
            except (ValueError, OSError) as exc:
                self._outcome(job, 'guide', 'warning', '导读未完成：' + _safe_error(str(exc)))
        else:
            self._outcome(job, 'guide', 'skipped', '未启用自动导读')
        if cancelled():
            self.jobs.cancel(job.id)
        else:
            outcomes = self.jobs.get(job.id).outcomes
            self.jobs.complete(job.id, warnings=any(item['status'] == 'warning' for item in outcomes.values()))
