from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import shutil

import httpx

from paper_library.catalog import CATALOG_COLUMNS, PaperPaths
from paper_library.config import ProjectPaths, Settings
from paper_library.translation.batch import load_tasks
from paper_library.translation.service import TranslationConfig, TranslationService
from paper_library.web.app import create_app


def make_settings(tmp_path: Path) -> Settings:
    paths = ProjectPaths.from_root(tmp_path)
    paths.libraries.mkdir()
    library = paths.libraries / "example"
    library.mkdir()
    (library / "library.toml").write_text(
        'schema_version = 1\nid = "example"\nname = "Example Library"\npaper_id_prefix = "ex"\n',
        encoding="utf-8",
    )
    paper = PaperPaths.for_id(library, "ex-p0001")
    paper.root.mkdir(parents=True)
    paper.pdf.write_bytes(b"%PDF-1.4\n")
    source = "# Example Paper\n\nAuthors\n\n# Abstract\n\nEnglish paragraph.\n"
    paper.markdown.write_text(source, encoding="utf-8")
    paper.metadata.write_text(json.dumps({"schema_version": 3}), encoding="utf-8")
    paper.images.mkdir()
    with (library / "catalog.tsv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t")
        writer.writerow(CATALOG_COLUMNS)
        writer.writerow(["ex-p0001", "2026", "ACL", "Example", "topic", "Example Paper", hashlib.sha256(paper.pdf.read_bytes()).hexdigest(), "2026-01-01T00:00:00+00:00", "2026-01-01", "legacy-id"])
    return Settings.load(
        paths,
        {"PAPER_TRANSLATION_CACHE": str(tmp_path / "translation-cache.sqlite3")},
    )


def test_reader_discovers_library_renders_paper_and_serves_assets(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    client = create_app(settings).test_client()

    assert client.get("/health/live").get_json() == {"status": "ok"}
    assert client.get("/health/ready").get_json() == {"status": "ok", "libraries": 1}
    assert "Example Library" in client.get("/").get_data(as_text=True)
    page = client.get("/library/example/paper/legacy-id")
    assert page.status_code == 308
    page = client.get("/library/example/paper/ex-p0001")
    assert page.status_code == 200
    assert "English paragraph" in page.get_data(as_text=True)
    assert client.get("/library/example/paper/ex-p0001/pdf").status_code == 200


def test_legacy_paper_url_redirects_to_stable_default_library(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    response = create_app(settings).test_client().get("/paper/mi-p0001")

    assert response.status_code == 308
    assert response.headers["Location"].endswith(
        "/library/mechanism-interpretability/paper/mi-p0001"
    )


def test_offline_cache_uses_stable_library_and_paper_identity(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    repository = create_app(settings).extensions["library_repository"]
    task = load_tasks(repository, "example")[0]

    def translate(_request):
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "译文"}}]})

    service = TranslationService(
        TranslationConfig("http://model/v1", "test-model", ""),
        settings.paths.cache,
        transport=httpx.MockTransport(translate),
    )
    service.translate(task.identity, task.version, task.segment)

    moved_root = tmp_path / "moved"
    moved_paths = ProjectPaths.from_root(moved_root)
    moved_root.mkdir()
    shutil.copytree(settings.paths.libraries, moved_paths.libraries)
    moved = load_tasks(create_app(Settings.load(moved_paths, {})).extensions["library_repository"], "example")[0]

    assert task.identity == "example/ex-p0001"
    assert moved.identity == task.identity
    assert service.cached(moved.identity, moved.version, moved.segment) == "译文"


def test_translation_endpoint_is_read_only_and_reports_missing_cache(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    client = create_app(settings).test_client()
    response = client.get("/library/example/paper/ex-p0001/translation")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["translated"] == 0
    assert payload["total"] > 0
    assert response.headers["Cache-Control"] == "no-store"
    assert client.post("/library/example/paper/ex-p0001/translation").status_code == 405


def test_reader_applies_only_current_valid_structure(tmp_path):
    from paper_library.structure.document import build_blocks, source_hash, VERSION
    settings = make_settings(tmp_path)
    paths = PaperPaths.for_id(settings.paths.libraries/'example','ex-p0001')
    source = paths.markdown.read_text()
    annotations = [{'id':b['id'],'role':'abstract' if 'English paragraph' in b['text'] else 'unknown'} for b in build_blocks(source)]
    sidecar = paths.root/'document-structure.json'
    sidecar.write_text(json.dumps({'version':VERSION,'source_sha256':source_hash(source),'annotations':annotations}))
    client = create_app(settings,start_jobs=False).test_client()
    url = '/library/example/paper/ex-p0001'
    before = client.get(url+'/translation').get_json()
    page = client.get(url).get_data(as_text=True)
    assert 'structure-abstract' in page
    assert client.get(url+'/translation').get_json() == before
    paths.markdown.write_text(source+'\nAdditional content.\n')
    assert 'class="structure-abstract"' not in client.get(url).get_data(as_text=True)
    sidecar.write_text('broken JSON')
    assert client.get(url).status_code == 200
