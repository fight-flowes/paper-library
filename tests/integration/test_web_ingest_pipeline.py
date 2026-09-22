from __future__ import annotations

import csv
from functools import partial
from io import BytesIO
from pathlib import Path

import httpx

from paper_library.catalog import CATALOG_COLUMNS, PaperPaths
from paper_library.config import ProjectPaths, Settings
from paper_library.ingest.service import ingest_pdf
from paper_library.jobs import JobRepository, JobStage, JobState, JobWorkflow
from paper_library.translation.batch import load_tasks
from paper_library.translation.service import TranslationConfig, TranslationService
from paper_library.web.app import create_app


class FakeMinerU:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, source, output, language, *, cancelled, on_event) -> None:
        self.calls += 1
        assert source.read_bytes().startswith(b"%PDF-")
        target = output / "fixture"
        (target / "images").mkdir(parents=True)
        (target / "article.md").write_text(
            "# Fixture paper\n\nEnglish paragraph for translation.\n",
            encoding="utf-8",
        )
        (target / "images" / "figure.png").write_bytes(b"image")
        on_event("fixture parsed")


def make_settings(tmp_path: Path) -> Settings:
    paths = ProjectPaths.from_root(tmp_path)
    for library_id, name, prefix in (
        ("whitespace", "Whitespace", "ws"),
        ("mechanism-interpretability", "Mechanism Interpretability", "mi"),
    ):
        library = paths.libraries / library_id
        library.mkdir(parents=True)
        (library / "library.toml").write_text(
            f'schema_version = 1\nid = "{library_id}"\nname = "{name}"\n'
            f'paper_id_prefix = "{prefix}"\n',
            encoding="utf-8",
        )
        with (library / "catalog.tsv").open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle, delimiter="\t", lineterminator="\n").writerow(CATALOG_COLUMNS)
    return Settings.load(
        paths,
        {"PAPER_TRANSLATION_CACHE": str(tmp_path / "cache" / "translations.sqlite3")},
    )


def test_complete_web_upload_parse_translate_and_read_pipeline(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings, start_jobs=False)
    client = app.test_client()
    token = app.extensions["management_guard"].token
    response = client.post(
        "/manage/jobs",
        data={
            "management_token": token,
            "library_id": "whitespace",
            "subject": "related-work",
            "language": "en",
            "auto_translate": "1",
            "pdf": (BytesIO(b"%PDF-fixture"), "2026-ACL-Fixture Fixture paper.pdf"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 303
    job_id = response.headers["Location"].rsplit("/", 1)[-1]

    def translate(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": "中文译文"}}]},
        )

    service = TranslationService(
        TranslationConfig("http://model/v1", "fixture-model", "", concurrency=4),
        settings.paths.cache,
        transport=httpx.MockTransport(translate),
        max_concurrency=4,
    )
    mineru = FakeMinerU()
    jobs = app.extensions["job_repository"]
    claimed = jobs.claim_next("integration", lease_seconds=30)
    workflow = JobWorkflow(
        jobs,
        app.extensions["upload_stager"],
        app.extensions["library_repository"],
        service,
        ingest=partial(ingest_pdf, mineru_runner=mineru),
    )

    workflow.run(claimed, cancelled=lambda: False)

    final = jobs.get(job_id)
    assert final.state is JobState.COMPLETED
    assert final.paper_id == "ws-p0001"
    paths = PaperPaths.for_id(settings.paths.libraries / "whitespace", "ws-p0001")
    assert paths.markdown.is_file()
    tasks = load_tasks(app.extensions["library_repository"], "whitespace", "ws-p0001")
    version = tasks[0].version
    records = service.cached_many(
        "whitespace/ws-p0001", version, [task.segment for task in tasks]
    )
    assert records
    assert client.get("/library/whitespace/paper/ws-p0001").status_code == 200
    assert mineru.calls == 1


def test_repository_recreation_resumes_published_job_at_translation(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings, start_jobs=False)
    client = app.test_client()
    token = app.extensions["management_guard"].token
    response = client.post(
        "/manage/jobs",
        data={
            "management_token": token,
            "library_id": "whitespace",
            "subject": "topic",
            "language": "en",
            "auto_translate": "1",
            "pdf": (BytesIO(b"%PDF-restart"), "restart.pdf"),
        },
        content_type="multipart/form-data",
    )
    job_id = response.headers["Location"].rsplit("/", 1)[-1]
    original = app.extensions["job_repository"]
    claimed = original.claim_next("dead", lease_seconds=1)

    library = settings.paths.libraries / "whitespace"
    paths = PaperPaths.for_id(library, "ws-p0001")
    paths.root.mkdir(parents=True)
    paths.pdf.write_bytes(b"%PDF-restart")
    paths.markdown.write_text("# Restart\n\nResume this paragraph.\n", encoding="utf-8")
    paths.metadata.write_text("{}\n", encoding="utf-8")
    paths.images.mkdir()
    with (library / "catalog.tsv").open("a", encoding="utf-8", newline="") as handle:
        csv.writer(handle, delimiter="\t", lineterminator="\n").writerow(
            ["ws-p0001", "", "", "", "topic", "Restart", "sha", "now", "", ""]
        )
    original.update(claimed.id, JobStage.PUBLISHED, paper_id="ws-p0001")

    recreated = JobRepository(settings.jobs_database)
    assert recreated.recover_expired() == 0
    # Explicitly retry the interrupted runner boundary, as startup recovery will
    # do after its lease expires.
    original.fail(job_id, "runner restarted")
    recreated.retry(job_id)
    resumed = recreated.claim_next("new", lease_seconds=30)

    service = TranslationService(
        TranslationConfig("http://model/v1", "fixture-model", "", concurrency=2),
        settings.paths.cache,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"choices": [{"finish_reason": "stop", "message": {"content": "恢复译文"}}]},
            )
        ),
        max_concurrency=2,
    )
    workflow = JobWorkflow(
        recreated,
        app.extensions["upload_stager"],
        app.extensions["library_repository"],
        service,
        ingest=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("MinerU reran")),
    )
    workflow.run(resumed, cancelled=lambda: False)

    assert recreated.get(job_id).state is JobState.COMPLETED_WITH_WARNINGS
    assert recreated.get(job_id).outcomes["repairing"]["status"] == "warning"
    assert len(app.extensions["library_repository"].load("whitespace").papers) == 1
