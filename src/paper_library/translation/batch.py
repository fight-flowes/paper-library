"""Bounded offline translation with per-segment persistence and resumable reports."""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
import hashlib
from pathlib import Path
from threading import Event

from paper_library.catalog import LibraryRepository, PaperPaths
from paper_library.web.markdown import render_translatable_markdown
from paper_library.translation.service import TranslationError, TranslationService
from paper_library.translation.segments import TranslationSegment


@dataclass(frozen=True)
class TranslationTask:
    library_id: str
    paper_id: str
    version: str
    segment: TranslationSegment

    @property
    def identity(self) -> str:
        return f"{self.library_id}/{self.paper_id}"


def load_tasks(repository: LibraryRepository, library_id: str, paper_id: str | None = None) -> list[TranslationTask]:
    snapshot = repository.load(library_id)
    if snapshot.warnings:
        raise ValueError('论文索引存在无效记录：' + '；'.join(snapshot.warnings))
    papers = snapshot.papers
    if paper_id is not None:
        paper = snapshot.find(paper_id)
        if paper is None:
            raise ValueError(f'论文库 {library_id} 中未找到论文 {paper_id}')
        papers = (paper,)
    tasks = []
    for paper in papers:
        path = PaperPaths.for_id(snapshot.descriptor.root, paper.id).markdown
        source = path.read_text(encoding='utf-8')
        version = hashlib.sha256(source.encode()).hexdigest()
        # IDs/text do not depend on this asset URL. The reader restores its own asset routes.
        rendered = render_translatable_markdown(source, '/asset')
        tasks.extend(TranslationTask(library_id, paper.id, version, segment) for segment in rendered.segments)
    return tasks


def run_batch(tasks, service: TranslationService, *, concurrency: int = 8, retries: int = 2,
              dry_run: bool = False, stop_event: Event | None = None, progress=print,
              on_update=None, retry_warnings: bool = False, retry_integrity: bool = False) -> dict:
    if not 1 <= concurrency <= 32:
        raise ValueError('并发数必须在 1 到 32 之间')
    if not 0 <= retries <= 5:
        raise ValueError('重试次数必须在 0 到 5 之间')
    stop = stop_event if stop_event is not None else Event()
    report = dict(total=len(tasks), cached=0, translated=0, failed=0, pending=0,
                  retried_warnings=0,
                  interrupted=False, model=service.config.model, concurrency=concurrency, results=[])

    def record(task, status, attempts=0, error=None):
        result = dict(library=task.library_id, paper_id=task.paper_id,
                      document_version=task.version, segment_id=task.segment.id,
                      status=status, attempts=attempts)
        if error:
            result['error'] = error
        return result

    pending = []
    warning_retries = set()
    for task in tasks:
        saved = service.cached_record(task.identity, task.version, task.segment)
        task_key = (task.identity, task.segment.id)
        if saved is not None and ((retry_warnings and saved.warnings)
                                  or (retry_integrity and task.segment.integrity_warnings(saved.text))):
            pending.append(task)
            warning_retries.add(task_key)
        elif saved is not None:
            report['cached'] += 1
            report['results'].append(record(task, 'cached'))
            report['results'][-1]['warnings'] = list(saved.warnings)
            report['results'][-1]['integrity_warnings'] = task.segment.integrity_warnings(saved.text)
        else:
            pending.append(task)
    progress(f"共 {len(tasks)} 段，已保存 {report['cached']} 段，待处理 {len(pending)} 段；并发 {concurrency}")
    report['pending'] = len(pending)
    if on_update:
        on_update(report, None)
    if dry_run or stop.is_set():
        report['pending'] = len(pending)
        report['interrupted'] = stop.is_set()
        report['results'].extend(record(task, 'pending') for task in pending)
        return report
    if pending and not service.config.configured:
        raise TranslationError(f'翻译模型尚未配置，请检查 {service.config.profile} 的配置。', 503)

    def translate(task):
        for attempt in range(1, retries + 2):
            if stop.is_set():
                return record(task, 'pending', attempt - 1)
            try:
                is_warning_retry = (task.identity, task.segment.id) in warning_retries
                translated = service.translate(task.identity, task.version, task.segment,
                                               force=is_warning_retry)
                result = record(task, 'translated', attempt)
                result['warnings'] = task.segment.warnings(translated)
                result['integrity_warnings'] = task.segment.integrity_warnings(translated)
                result['retried_warning'] = is_warning_retry
                return result
            except TranslationError as error:
                if attempt > retries or error.status not in {429, 502, 503}:
                    return record(task, 'failed', attempt, str(error))
                if stop.wait(min(2 ** (attempt - 1), 4)):
                    return record(task, 'pending', attempt)
        raise AssertionError('unreachable')

    remaining = iter(pending)
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix='translate') as pool:
        active = {}
        def fill():
            while len(active) < concurrency and not stop.is_set():
                task = next(remaining, None)
                if task is None:
                    break
                active[pool.submit(translate, task)] = task
        fill()
        while active:
            completed, _ = wait(active, return_when=FIRST_COMPLETED)
            for future in completed:
                task = active.pop(future)
                result = future.result()
                report[result['status']] += 1
                if result.get('retried_warning') and result['status'] == 'translated':
                    report['retried_warnings'] += 1
                report['results'].append(result)
                done = report['cached'] + report['translated'] + report['failed']
                message = f"[{done}/{report['total']}] {task.library_id}/{task.paper_id} {task.segment.id}: {result['status']}"
                if result.get('error'):
                    message += ' — ' + result['error']
                report['pending'] = max(0, report['total'] - report['cached'] - report['translated'] - report['failed'])
                if on_update:
                    on_update(report, message if result.get('error') else None)
                else:
                    progress(message)
            fill()
    for task in remaining:
        report['pending'] += 1
        report['results'].append(record(task, 'pending'))
    report['pending'] = max(0, report['total'] - report['cached'] - report['translated'] - report['failed'])
    report['interrupted'] = stop.is_set()
    return report
