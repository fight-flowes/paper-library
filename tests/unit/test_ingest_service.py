from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from paper_library.catalog import CATALOG_COLUMNS, LibraryRepository, PaperPaths
from paper_library.ingest.service import IngestOutcome, IngestResult, ingest_pdf


def write_library(root: Path, pdf: Path) -> tuple[LibraryRepository, Path]:
    library = root / "whitespace"
    library.mkdir(parents=True)
    (library / "library.toml").write_text(
        'schema_version = 1\nid = "whitespace"\nname = "Whitespace"\n'
        'paper_id_prefix = "ws"\n',
        encoding="utf-8",
    )
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    with (library / "catalog.tsv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(CATALOG_COLUMNS)
        writer.writerow(
            ["ws-p0001", "2024", "ACL", "Old", "existing", "Old paper", digest,
             "2026-01-01T00:00:00+00:00", "2024-01-01", ""]
        )
    paths = PaperPaths.for_id(library, "ws-p0001")
    paths.root.mkdir(parents=True)
    paths.pdf.write_bytes(pdf.read_bytes())
    paths.markdown.write_text("# Old paper\n", encoding="utf-8")
    paths.metadata.write_text('{"old": true}\n', encoding="utf-8")
    paths.images.mkdir()
    return LibraryRepository(root), library


class SuccessfulRunner:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, source, output, language, *, cancelled, on_event) -> None:
        self.calls += 1
        assert not cancelled()
        target = output / "paper"
        (target / "images").mkdir(parents=True)
        (target / "article.md").write_text("# New paper\n\nContent", encoding="utf-8")
        (target / "images" / "figure.png").write_bytes(b"png")
        on_event("parsed")


def test_duplicate_pdf_returns_existing_paper_without_running_mineru(tmp_path: Path) -> None:
    pdf = tmp_path / "2024-ACL-Old Old paper.pdf"
    pdf.write_bytes(b"%PDF-existing")
    repository, library = write_library(tmp_path / "libraries", pdf)
    runner = Mock()

    result = ingest_pdf(
        repository,
        "whitespace",
        pdf,
        mineru_runner=runner,
        enrich=False,
    )

    assert result == IngestResult(
        "whitespace",
        "ws-p0001",
        IngestOutcome.UNCHANGED,
        PaperPaths.for_id(library, "ws-p0001"),
    )
    runner.run.assert_not_called()


def test_successful_ingest_returns_created_result_and_valid_package(tmp_path: Path) -> None:
    existing = tmp_path / "existing.pdf"
    existing.write_bytes(b"%PDF-existing")
    repository, library = write_library(tmp_path / "libraries", existing)
    source = tmp_path / "2026-ACL-New Useful Paper.pdf"
    source.write_bytes(b"%PDF-new")
    runner = SuccessfulRunner()

    result = ingest_pdf(
        repository,
        "whitespace",
        source,
        subject="mechanisms",
        mineru_runner=runner,
        enrich=False,
    )

    assert result.outcome is IngestOutcome.CREATED
    assert result.paper_id == "ws-p0002"
    assert result.paths.markdown.read_text(encoding="utf-8").startswith("# New")
    assert repository.load("whitespace").find("ws-p0002") is not None
    metadata = json.loads(result.paths.metadata.read_text(encoding="utf-8"))
    assert metadata["enrichment"]["status"] == "skipped"
    assert not list((library / "papers").glob(".paper-staging-*"))


def test_publication_failure_restores_package_and_catalog(tmp_path: Path) -> None:
    source = tmp_path / "2024-ACL-Old Old paper.pdf"
    source.write_bytes(b"%PDF-existing")
    repository, library = write_library(tmp_path / "libraries", source)
    old_paths = PaperPaths.for_id(library, "ws-p0001")
    old_package = old_paths.markdown.read_bytes()
    old_catalog = (library / "catalog.tsv").read_bytes()

    def fail_catalog(_source: Path, _target: Path) -> None:
        raise OSError("simulated catalog failure")

    with pytest.raises(OSError, match="simulated"):
        ingest_pdf(
            repository,
            "whitespace",
            source,
            force=True,
            mineru_runner=SuccessfulRunner(),
            catalog_publisher=fail_catalog,
            enrich=False,
        )

    assert old_paths.markdown.read_bytes() == old_package
    assert (library / "catalog.tsv").read_bytes() == old_catalog
    assert repository.load("whitespace").find("ws-p0001") is not None
    assert not list(library.glob(".catalog-backup-*"))
    assert not list((library / "papers").glob(".*backup*"))
    assert not list((library / "papers").glob(".paper-staging-*"))


def test_runner_failure_and_preexisting_cancellation_leave_no_staging(tmp_path: Path) -> None:
    existing = tmp_path / "existing.pdf"
    existing.write_bytes(b"%PDF-existing")
    repository, library = write_library(tmp_path / "libraries", existing)
    source = tmp_path / "new.pdf"
    source.write_bytes(b"%PDF-new")

    with pytest.raises(ValueError, match="取消"):
        ingest_pdf(
            repository,
            "whitespace",
            source,
            mineru_runner=SuccessfulRunner(),
            cancelled=lambda: True,
            enrich=False,
        )

    assert not list((library / "papers").glob(".paper-staging-*"))


def test_invalid_mineru_output_is_rejected(tmp_path: Path) -> None:
    existing = tmp_path / "existing.pdf"
    existing.write_bytes(b"%PDF-existing")
    repository, library = write_library(tmp_path / "libraries", existing)
    source = tmp_path / "new.pdf"
    source.write_bytes(b"%PDF-new")

    class EmptyRunner:
        def run(self, source, output, language, *, cancelled, on_event) -> None:
            output.mkdir(parents=True)

    with pytest.raises(ValueError, match="Markdown"):
        ingest_pdf(
            repository,
            "whitespace",
            source,
            mineru_runner=EmptyRunner(),
            enrich=False,
        )

    assert not list((library / "papers").glob(".paper-staging-*"))


def test_backup_cleanup_failure_does_not_roll_back_published_update(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "2024-ACL-Old Old paper.pdf"
    source.write_bytes(b"%PDF-existing")
    repository, library = write_library(tmp_path / "libraries", source)
    real_rmtree = __import__("shutil").rmtree

    def fail_backup_cleanup(path, *args, **kwargs):
        if ".backup-" in Path(path).name and not kwargs.get("ignore_errors"):
            raise OSError("cleanup unavailable")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr("paper_library.ingest.service.shutil.rmtree", fail_backup_cleanup)

    result = ingest_pdf(
        repository,
        "whitespace",
        source,
        force=True,
        mineru_runner=SuccessfulRunner(),
        enrich=False,
    )

    assert result.outcome is IngestOutcome.UPDATED
    assert result.paths.markdown.read_text(encoding="utf-8").startswith("# New")
    assert repository.load("whitespace").find("ws-p0001") is not None


def test_failed_rollback_retains_recovery_copies(tmp_path, monkeypatch):
    from paper_library.storage.publication import recover_publications
    import os
    source=tmp_path/'old.pdf';source.write_bytes(b'%PDF-existing')
    repository,library=write_library(tmp_path/'libraries',source)
    original=os.replace
    def broken_restore(src,dst):
        if Path(src).name=='old-package':raise OSError('restore failed')
        return original(src,dst)
    def broken_publish(*args):raise OSError('publish failed')
    with monkeypatch.context() as patcher:
        patcher.setattr(os,'replace',broken_restore)
        with pytest.raises(OSError,match='restore failed'):
            ingest_pdf(repository,'whitespace',source,force=True,enrich=False,mineru_runner=SuccessfulRunner(),catalog_publisher=broken_publish)
    assert list((library/'.transactions').glob('*/old-package/article.md'))
    assert (library/'catalog.tsv').exists()
    recover_publications(library)
    assert (library/'papers/ws-p0001/article.md').read_text()=='# Old paper\n'
    assert not list((library/'.transactions').glob('*'))
