"""Transactional PDF ingestion for normalized paper packages."""

from __future__ import annotations

from contextlib import contextmanager
from collections import deque
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from collections.abc import Callable
from queue import Empty, Queue
from uuid import uuid4

from paper_library.catalog import CATALOG_COLUMNS, LibraryRepository, PaperPaths
from paper_library.metadata.service import Gateway, GatewayClient, enrich_bibliography


def _never() -> bool:
    return False


def _ignore(_event: str) -> None:
    return None


class IngestOutcome(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


@dataclass(frozen=True, slots=True)
class IngestResult:
    library_id: str
    paper_id: str
    outcome: IngestOutcome
    paths: PaperPaths


class MinerURunner:
    """Supervise the remote transport; never execute a local PDF parser."""

    def __init__(self, settings=None) -> None:
        from paper_library.config import default_settings
        self.settings = settings or default_settings()
        self.last_output: tuple[str, ...] = ()
        self.last_log: Path | None = None

    def run(
        self,
        source: Path,
        output: Path,
        language: str,
        *,
        cancelled: Callable[[], bool] = _never,
        on_event: Callable[[str], None] = _ignore,
    ) -> None:
        from .runtime import parser_command
        parser_command(self.settings.values)
        timeout = float(self.settings.values.get('PAPER_MINERU_TIMEOUT_SECONDS', '21600'))
        idle_timeout = float(self.settings.values.get('PAPER_MINERU_IDLE_TIMEOUT_SECONDS', '0'))
        if timeout <= 0 or idle_timeout < 0:
            raise ValueError('解析总超时必须为正数，空闲超时不能为负数')
        environment = os.environ.copy()
        for key, value in self.settings.values.items():
            if key.startswith('PAPER_MINERU_'):
                environment[key] = value
        environment['MINERU_MODEL_SOURCE'] = self.settings.values.get('PAPER_MINERU_MODEL_SOURCE', 'local')
        environment['MINERU_PROCESSING_WINDOW_SIZE'] = self.settings.values.get(
            'PAPER_MINERU_PROCESSING_WINDOW_SIZE', '8'
        )
        environment['MINERU_HYBRID_BATCH_RATIO'] = self.settings.values.get(
            'PAPER_MINERU_HYBRID_BATCH_RATIO', '1'
        )
        log_directory = self.settings.paths.state / 'logs' / 'mineru'
        log_handle = None
        try:
            log_directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            self.last_log = log_directory / f'{stamp}-{uuid4().hex[:8]}.log'
            log_handle = self.last_log.open('w', encoding='utf-8')
        except OSError:
            self.last_log = None
        process = subprocess.Popen(
            [sys.executable, '-m', 'paper_library.ingest.remote',
             '-p', str(source), '-o', str(output), '-l', language],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            start_new_session=True,
            bufsize=1,
        )
        lines: Queue[str | None] = Queue(maxsize=201)

        def read_output() -> None:
            assert process.stdout is not None
            try:
                for line in process.stdout:
                    lines.put(line)
            finally:
                lines.put(None)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        captured: deque[str] = deque(maxlen=200)
        last_event = 0.0
        ended = False
        started = last_output_at = time.monotonic()
        try:
            while not ended or process.poll() is None:
                now = time.monotonic()
                if now - started > timeout:
                    raise ValueError(f'MinerU 解析总超时（{timeout:g} 秒），可调整 PAPER_MINERU_TIMEOUT_SECONDS 后重试')
                if idle_timeout and now - last_output_at > idle_timeout:
                    raise ValueError(f'MinerU 连续 {idle_timeout:g} 秒没有输出，可调整或禁用空闲超时后重试')
                if cancelled():
                    self._terminate(process)
                    raise ValueError("MinerU 解析已取消")
                try:
                    line = lines.get(timeout=0.1)
                except Empty:
                    continue
                last_output_at = time.monotonic()
                if line is None:
                    ended = True
                    continue
                sanitized = re.sub(r"\s+", " ", line).strip()[:300]
                if not sanitized:
                    continue
                captured.append(sanitized)
                if log_handle is not None:
                    log_handle.write(sanitized + '\n')
                    log_handle.flush()
                now = time.monotonic()
                if now - last_event >= 0.25:
                    on_event(sanitized)
                    last_event = now
            return_code = process.wait()
            if return_code:
                raise ValueError(f"cicore01 远程解析失败，退出码 {return_code}；禁止回退本机解析")
        finally:
            self.last_output = tuple(captured)
            if process.poll() is None:
                self._terminate(process)
            reader.join(timeout=1)
            if log_handle is not None:
                log_handle.close()
            self._rotate_logs(log_directory)

    def _rotate_logs(self, directory: Path) -> None:
        keep = int(self.settings.values.get('PAPER_MINERU_LOG_KEEP', '10'))
        try:
            logs = sorted(directory.glob('*.log'), key=lambda path: path.stat().st_mtime, reverse=True)
            for path in logs[keep:]:
                path.unlink(missing_ok=True)
        except OSError:
            return

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        process.wait(timeout=5)


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _identity(stem: str) -> tuple[str, str, str, str]:
    first, separator, remainder = stem.partition(" ")
    match = re.fullmatch(r"(?P<year>\d{4})-(?P<venue>[A-Za-z0-9]+)-(?P<short>[A-Za-z0-9][A-Za-z0-9._-]*)", first)
    if match:
        return remainder.strip() if separator else stem, match["year"], match["venue"], match["short"]
    return stem, "", "", ""


@contextmanager
def _lock(library_root: Path):
    handle = (library_root / ".catalog.lock").open("a")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("该论文库正在更新") from error
        from paper_library.storage.publication import recover_publications
        recover_publications(library_root)
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def _next_id(snapshot) -> str:
    prefix = snapshot.descriptor.config.paper_id_prefix
    pattern = re.compile(rf"^{re.escape(prefix)}-p(\d+)$")
    reserved = {key.casefold() for paper in snapshot.papers for key in (paper.id, *paper.aliases)}
    directory = snapshot.descriptor.root / 'papers'
    if directory.is_dir():
        reserved.update(path.name.casefold() for path in directory.iterdir())
    highest = max((int(match.group(1)) for key in reserved if (match := pattern.fullmatch(key))), default=0)
    return f"{prefix}-p{highest + 1:04d}"


def _sanitize_markdown(value: str) -> str:
    return "".join(character for character in value if character in "\t\n\r" or unicodedata.category(character) != "Cc")


def _catalog_rows(snapshot, added: dict[str, str] | None = None) -> list[dict[str, str]]:
    _require_clean_catalog(snapshot)
    rows = [
        {
            "id": paper.id, "year": paper.year, "venue": paper.venue,
            "short_name": paper.short_name, "subject": paper.subject,
            "title": paper.title, "sha256": paper.sha256,
            "parsed_at": paper.parsed_at, "publication_date": paper.publication_date,
            "aliases": "|".join(paper.aliases),
        }
        for paper in snapshot.papers
    ]
    if added:
        rows.append(added)
    return sorted(rows, key=lambda row: (row["subject"].casefold(), row["year"], row["id"]))


def _require_clean_catalog(snapshot):
    if snapshot.warnings:
        raise ValueError('论文目录存在无效记录，已停止写入以保护现有论文：' + '；'.join(snapshot.warnings))


def _write_catalog(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CATALOG_COLUMNS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _enrichment(
    title: str,
    year: str,
    venue: str,
    enabled: bool,
    gateway: Gateway | None = None,
) -> dict[str, object]:
    if not enabled:
        return {"status": "skipped", "confidence": None, "bibliography": {}, "message": "未执行外部元数据检索"}
    result = enrich_bibliography(title, year, venue, gateway or GatewayClient())
    return {
        "status": result.status, "confidence": result.confidence,
        "bibliography": result.bibliography, **result.enrichment,
    }


def ingest_pdf(
    repository: LibraryRepository,
    library_id: str,
    pdf: Path,
    *,
    subject: str = "",
    language: str = "en",
    force: bool = False,
    dry_run: bool = False,
    enrich: bool = True,
    mineru_runner: MinerURunner | None = None,
    cancelled: Callable[[], bool] = _never,
    on_event: Callable[[str], None] = _ignore,
    catalog_publisher: Callable[[Path, Path], None] = os.replace,
    gateway: Gateway | None = None,
    original_filename: str | None = None,
    repair_settings=None,
    on_progress: Callable[[str, dict], None] = lambda stage, detail: None,
) -> IngestResult:
    source = pdf.expanduser().resolve(strict=True)
    if not source.is_file() or source.suffix.casefold() != ".pdf":
        raise ValueError("输入必须是 PDF 文件")
    title, year, venue, short_name = _identity(Path(original_filename or source.name).stem)
    digest = _digest(source)
    snapshot = repository.load(library_id)
    root = snapshot.descriptor.root
    runner = mineru_runner or MinerURunner(repair_settings)
    with _lock(root):
        snapshot = repository.load(library_id)
        _require_clean_catalog(snapshot)
        duplicate = next((paper for paper in snapshot.papers if paper.sha256 == digest), None)
        paper_id = duplicate.id if duplicate else _next_id(snapshot)
        paths = PaperPaths.for_id(root, paper_id)
        if paths.root.is_symlink() or (not duplicate and paths.root.exists()):
            raise ValueError('新论文编号与现有论文包冲突，已停止导入')
        if duplicate and force and paths.pdf.is_file() and _digest(paths.pdf) != digest:
            raise ValueError('现有 PDF 与目录指纹不一致，已停止覆盖，请先核对论文包')
        if duplicate and not force:
            return IngestResult(library_id, paper_id, IngestOutcome.UNCHANGED, paths)
        outcome = IngestOutcome.UPDATED if duplicate else IngestOutcome.CREATED
        if dry_run:
            return IngestResult(library_id, paper_id, outcome, paths)
        if cancelled():
            raise ValueError("PDF 导入已取消")
        destination = PaperPaths.for_id(root, paper_id).root
        (root / "papers").mkdir(exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".paper-staging-", dir=root / "papers"))
        catalog_staging: Path | None = None
        try:
            if destination.exists():
                from paper_library.storage.history import checkpoint
                checkpoint(destination, 'before reparse')
                if (destination / '.history').exists():
                    shutil.copytree(destination / '.history', staging / '.history', dirs_exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="paper-mineru-") as temporary:
                output = Path(temporary) / "output"
                on_progress("parsing", {"message": "正在由 cicore01 远程解析 PDF"})
                on_event("正在由 cicore01 远程解析 PDF")
                runner.run(
                    source,
                    output,
                    language,
                    cancelled=cancelled,
                    on_event=on_event,
                )
                if cancelled():
                    raise ValueError("PDF 导入已取消")
                markdowns = [path for path in output.rglob("*.md") if path.is_file() and path.stat().st_size]
                if len(markdowns) != 1:
                    raise ValueError(f"MinerU 应生成一个 Markdown，实际生成 {len(markdowns)} 个")
                markdown = _sanitize_markdown(markdowns[0].read_text(encoding="utf-8"))
                if not markdown.strip():
                    raise ValueError("MinerU 生成的 Markdown 为空")
                shutil.copy2(source, staging / "source.pdf")
                (staging / "article.md").write_text(markdown, encoding="utf-8")
                for name in ('pdf-frontmatter.json', 'parser-provenance.json'):
                    if (output / name).is_file():
                        shutil.copy2(output / name, staging / name)
                from paper_library.storage.history import checkpoint
                checkpoint(staging, "MinerU original")
                from paper_library.ingest.paragraphs import repair_file
                on_event("正在检查段落连续性")
                repair_file(staging / "article.md", settings=repair_settings, cancelled=cancelled,
                            on_progress=lambda done, total: on_progress("repairing", {
                                "message": "正在检查段落连续性" if total else "没有需要检查的断点",
                                "done": done, "total": total, "unit": "处"}))
                images = staging / "images"
                source_images = markdowns[0].parent / "images"
                if source_images.is_dir():
                    shutil.copytree(source_images, images)
                else:
                    images.mkdir()
            try:
                parser_provenance = json.loads((staging / 'parser-provenance.json').read_text())
                mineru_version = parser_provenance['mineru_version']
            except (OSError, ValueError, KeyError):
                parser_provenance = {}
                mineru_version = "unknown"
            parsed_at = datetime.now(timezone.utc).isoformat()
            from paper_library.metadata.repair import verify_bibliography, repaired_short_name, parsed_display_title
            from paper_library.metadata.identifiers import ACLClient, CrossrefClient
            if enrich:
                on_progress("enriching", {"message": "正在检索并核对论文信息"})
                result = verify_bibliography(
                    title, year, venue, original_filename or source.name,
                    (staging / 'article.md').read_text(encoding='utf-8'),
                    gateway or GatewayClient(), pdf=staging / 'source.pdf', settings=repair_settings,
                    official_lookup=CrossrefClient() if gateway is None else None, cancelled=cancelled,
                    acl_lookup=ACLClient() if gateway is None else None,
                )
                enrichment = {'status': result.status, 'confidence': result.confidence,
                              'bibliography': result.bibliography, **result.enrichment}
            else:
                enrichment = _enrichment(title, year, venue, False, gateway)
            bibliography = enrichment.pop("bibliography", {})
            title = parsed_display_title(title, (staging / 'article.md').read_text(encoding='utf-8'))
            if enrichment.get("status") == "verified" and isinstance(bibliography, dict):
                title = str(bibliography.get("canonical_title") or title)
                short_name = repaired_short_name(short_name, title)
                venue = str(bibliography.get("venue") or venue)
                publication_date = str(bibliography.get("publication_date") or "")
                year = publication_date[:4] if re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", publication_date) else year
            else:
                publication_date = ""
            on_progress("publishing", {"message": "正在保存论文并更新目录"})
            metadata = {
                "schema_version": 3, "id": paper_id, "title": title,
                "year": year, "venue": venue, "short_name": short_name,
                "subject": subject, "original_filename": original_filename or source.name,
                "source_sha256": digest, "source_size": source.stat().st_size,
                "language": language, "mineru_version": mineru_version,
                "parser": parser_provenance,
                "parsed_at": parsed_at, "bibliography": bibliography,
                "enrichment": enrichment,
            }
            (staging / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            row = {"id": paper_id, "year": year, "venue": venue, "short_name": short_name, "subject": subject, "title": title, "sha256": digest, "parsed_at": parsed_at, "publication_date": publication_date, "aliases": ""}
            rows = [item for item in _catalog_rows(snapshot) if item["id"] != paper_id]
            rows.append(row)
            with tempfile.NamedTemporaryFile(dir=root, prefix=".catalog-staging-", delete=False) as handle:
                catalog_staging = Path(handle.name)
            _write_catalog(catalog_staging, sorted(rows, key=lambda item: (item["subject"].casefold(), item["year"], item["id"])))
            if cancelled():
                raise ValueError("PDF 导入已取消")
            on_event("正在发布论文")
            from paper_library.storage.publication import publish
            def validate():
                if repository.load(library_id).find(paper_id) is None:
                    raise ValueError("发布后的目录未包含新论文")
            publish(root, paper_id, staging, catalog_staging, catalog_publisher, validate)
            catalog_staging = None
            return IngestResult(library_id, paper_id, outcome, PaperPaths.for_id(root, paper_id))
        finally:
            # Failed rollback retains its package under .transactions.
            if staging.exists() and not any((root / '.transactions').glob('*/journal.json')):
                shutil.rmtree(staging, ignore_errors=True)
            if catalog_staging:
                catalog_staging.unlink(missing_ok=True)


def _merge_bibliography(existing: dict, verified: dict) -> dict:
    merged = {**existing, **verified}
    # A preprint DOI identifies a different version; do not cite it as the
    # conference or journal article when the official page has no DOI.
    if (verified.get('venue_type') in {'conference', 'journal'}
            and not verified.get('doi')
            and re.fullmatch(r'10\.48550/arXiv\.\d{4}\.\d{4,5}',
                             str(existing.get('doi', '')), re.I)):
        merged.pop('doi', None)
    return merged


def refresh_metadata(repository: LibraryRepository, library_id: str, paper_id: str, *, gateway=None,
                     settings=None, cancelled=lambda: False) -> str:
    from paper_library.metadata.repair import verify_bibliography, repaired_short_name, leading_split_title
    from paper_library.metadata.identifiers import ACLClient, CrossrefClient
    from paper_library.metadata.service import _normalize
    from paper_library.structure.frontmatter import source_headings

    snapshot = repository.load(library_id)
    paper = snapshot.find(paper_id)
    if paper is None:
        raise ValueError(f"未找到论文：{library_id}/{paper_id}")
    paths = PaperPaths.for_id(snapshot.descriptor.root, paper.id)
    with _lock(snapshot.descriptor.root):
        snapshot = repository.load(library_id)
        _require_clean_catalog(snapshot)
        paper = snapshot.find(paper.id)
        metadata = json.loads(paths.metadata.read_text(encoding="utf-8"))
        markdown = paths.markdown.read_text(encoding='utf-8')
        result = verify_bibliography(
            paper.title, paper.year, paper.venue, metadata.get('original_filename', ''),
            markdown, gateway or GatewayClient(), pdf=paths.pdf, known=metadata.get('bibliography'),
            settings=settings, official_lookup=CrossrefClient() if gateway is None else None,
            acl_lookup=ACLClient() if gateway is None else None,
            cancelled=cancelled,
        )
        audit = paths.root / 'metadata-repairs' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '-' + uuid4().hex[:8])
        audit.mkdir(parents=True)
        originals = {paths.metadata: paths.metadata.read_bytes(),
                     paths.markdown: paths.markdown.read_bytes(),
                     snapshot.descriptor.root / 'catalog.tsv': (snapshot.descriptor.root / 'catalog.tsv').read_bytes()}
        for path, data in originals.items():
            (audit / path.name).write_bytes(data)
        report = {'status': result.status, 'confidence': result.confidence,
                  'bibliography': result.bibliography, 'evidence': result.enrichment}
        (audit / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        if result.status != 'verified':
            return f"未修改：{library_id}/{paper.id}（{result.status}，已保存核验记录）"
        bibliography = result.bibliography
        canonical = ' '.join(str(bibliography['canonical_title']).split())
        venue = ' '.join(str(bibliography.get('venue') or paper.venue).split())
        date = str(bibliography.get('publication_date') or paper.publication_date)
        if date and not re.fullmatch(r'\d{4}(?:-\d{2}(?:-\d{2})?)?', date):
            date = paper.publication_date
        fields = {'title': canonical, 'short_name': repaired_short_name(paper.short_name, canonical),
                  'venue': venue, 'publication_date': date, 'year': date[:4] if date else paper.year}
        metadata.update(fields)
        metadata['bibliography'] = _merge_bibliography(metadata.get('bibliography', {}), bibliography)
        metadata['enrichment'] = {'status': result.status, 'confidence': result.confidence, **result.enrichment}
        visible = source_headings(markdown)
        offset = sum(map(len, markdown.splitlines(keepends=True)[:visible[0].start_line - 1])) if visible else 0
        heading = re.match(r'^#{1,6}[ \t]+(.+?)[ \t]*(?:\n|$)', markdown[offset:]) if visible else None
        if (heading and _normalize(heading.group(1)) != _normalize(canonical)
                and _normalize(leading_split_title(markdown)) != _normalize(canonical)):
            old = _normalize(heading.group(1))
            if len(old.split()) >= 4 and (old in _normalize(canonical) or old == _normalize(paper.title)):
                markdown = markdown[:offset + heading.start(1)] + canonical + markdown[offset + heading.end(1):]
        rows = _catalog_rows(snapshot)
        for row in rows:
            if row['id'] == paper.id:
                row.update(fields)
        from paper_library.storage.publication import publish_files
        with tempfile.TemporaryDirectory(prefix='.metadata-staging-', dir=snapshot.descriptor.root) as temporary:
            catalog = Path(temporary) / 'catalog.tsv'
            _write_catalog(catalog, rows)
            replacements = {'metadata.json': (json.dumps(metadata, ensure_ascii=False, indent=2) + '\n').encode(),
                            'article.md': markdown.encode(), 'catalog.tsv': catalog.read_bytes()}
            def validate():
                refreshed = repository.load(library_id)
                _require_clean_catalog(refreshed)
                if refreshed.find(paper.id).title != canonical:
                    raise ValueError('目录更新校验失败')
            publish_files(snapshot.descriptor.root, paper.id, replacements, validate)
    return f"已核验并同步：{library_id}/{paper.id} — {canonical}"
