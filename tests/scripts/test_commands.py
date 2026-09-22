from pathlib import Path

import pytest

from paper_library.cli import dev, library, translate
from paper_library.catalog import PaperPaths
from paper_library.ingest.service import IngestOutcome, IngestResult
from paper_library.catalog import CATALOG_COLUMNS
from paper_library.config import ProjectPaths, Settings
from paper_library.jobs import JobRepository, NewJob
from paper_library.jobs.security import ManagementGuard
import json


def test_public_commands_are_extensionless_and_executable() -> None:
    root = Path(__file__).resolve().parents[2]
    for name in ("dev", "library", "translate"):
        path = root / "scripts" / name
        assert path.is_file()
        assert path.stat().st_mode & 0o111


def test_legacy_shell_entrypoints_are_absent() -> None:
    root = Path(__file__).resolve().parents[2]
    for name in ("reader.sh", "translate.sh", "parse_pdf.sh"):
        assert not (root / "scripts" / name).exists()


@pytest.mark.parametrize("entrypoint", (dev.main, library.main, translate.main))
def test_no_arguments_prints_help_and_succeeds(entrypoint, capsys) -> None:
    assert entrypoint([]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert output.out.startswith("usage: scripts/")


@pytest.mark.parametrize(
    ("outcome", "label"),
    [
        (IngestOutcome.CREATED, "已导入"),
        (IngestOutcome.UPDATED, "已更新"),
        (IngestOutcome.UNCHANGED, "未变更"),
    ],
)
def test_ingest_formats_structured_result(
    tmp_path: Path, monkeypatch, capsys, outcome: IngestOutcome, label: str
) -> None:
    result = IngestResult(
        "whitespace",
        "ws-p0007",
        outcome,
        PaperPaths.for_id(tmp_path, "ws-p0007"),
    )
    monkeypatch.setattr("paper_library.ingest.service.ingest_pdf", lambda *args, **kwargs: result)

    assert library.main(["ingest", "--library", "whitespace", str(tmp_path / "paper.pdf")]) == 0

    output = capsys.readouterr().out.strip()
    assert output == f"{label}：whitespace/ws-p0007"
    assert str(tmp_path) not in output


def test_dev_doctor_reports_safe_job_diagnostics(tmp_path: Path, monkeypatch, capsys) -> None:
    paths = ProjectPaths.from_root(tmp_path)
    library_root = paths.libraries / "whitespace"
    library_root.mkdir(parents=True)
    (library_root / "library.toml").write_text(
        'schema_version = 1\nid = "whitespace"\nname = "Whitespace"\n'
        'paper_id_prefix = "ws"\n', encoding="utf-8"
    )
    (library_root / "catalog.tsv").write_text("\t".join(CATALOG_COLUMNS) + "\n", encoding="utf-8")
    settings = Settings.load(paths, {})
    jobs = JobRepository(settings.jobs_database)
    jobs.create(NewJob("private.pdf", "whitespace", "topic", "en", False, True,
                       "secret-upload/source.pdf"))
    token = ManagementGuard.from_state_directory(paths.state, settings.host, False).token
    monkeypatch.setattr(dev, "default_settings", lambda: settings)
    monkeypatch.setattr(dev, "_listener_pids", lambda _port: set())

    assert dev.main(["doctor"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["jobs_database_accessible"] is True
    assert payload["queued_jobs"] == 1
    assert payload["active_jobs"] == 0
    assert payload["upload_max_mb"] == 256
    assert payload["management_mutations_enabled"] is True
    serialized = json.dumps(payload)
    assert token not in serialized
    assert "secret-upload" not in serialized
    assert "API_KEY" not in serialized


def test_read_only_guard_does_not_lock_or_checkpoint(monkeypatch):
    from paper_library.cli import guarded
    from unittest.mock import Mock
    call=Mock();monkeypatch.setattr(guarded.runpy,'run_module',call)
    monkeypatch.setattr(guarded,'default_settings',lambda: (_ for _ in ()).throw(AssertionError('read-only must not lock')))
    for args in [[],['list'],['check'],['--help'],['run','--help']]:
        monkeypatch.setattr(guarded.sys,'argv',['guarded','paper_library.cli.library',*args]);guarded.main()
    assert call.call_count==5
