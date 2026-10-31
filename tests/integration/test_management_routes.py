from __future__ import annotations

from io import BytesIO
from pathlib import Path
import re

from paper_library.catalog import CATALOG_COLUMNS
from paper_library.config import ProjectPaths, Settings
from paper_library.web.app import create_app


def make_settings(tmp_path: Path, **values: str) -> Settings:
    paths = ProjectPaths.from_root(tmp_path)
    library = paths.libraries / "whitespace"
    library.mkdir(parents=True)
    (library / "library.toml").write_text(
        'schema_version = 1\nid = "whitespace"\nname = "Whitespace"\n'
        'paper_id_prefix = "ws"\n', encoding="utf-8"
    )
    (library / "catalog.tsv").write_text("\t".join(CATALOG_COLUMNS) + "\n", encoding="utf-8")
    return Settings.load(paths, values)


def upload(client, token: str, **changes):
    data = {
        "management_token": token,
        "library_id": "whitespace",
        "subject": "related-work",
        "language": "en",
        "enrich": "1",
        "auto_translate": "1",
        "pdf": (BytesIO(b"%PDF-fixture"), "paper.pdf"),
    }
    data.update(changes)
    return client.post("/manage/jobs", data=data, content_type="multipart/form-data")


def test_import_page_and_upload_create_durable_job(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path), start_jobs=False)
    client = app.test_client()
    token = app.extensions["management_guard"].token

    page = client.get("/manage/import")
    response = upload(client, token)

    assert page.status_code == 200
    assert "Whitespace" in page.get_data(as_text=True)
    assert 'aria-label="关闭导入界面"' not in page.get_data(as_text=True)
    assert response.status_code == 303
    assert re.search(r"/manage/jobs/[0-9a-f]+$", response.headers["Location"])
    job_id = response.headers["Location"].rsplit("/", 1)[-1]
    assert app.extensions["job_repository"].get(job_id) is not None


def test_subject_creation_persists_and_reuses_duplicates(tmp_path: Path):
    settings = make_settings(tmp_path)
    app = create_app(settings, start_jobs=False)
    client = app.test_client()
    data = {'management_token': app.extensions['management_guard'].token,
            'library_id': 'whitespace', 'subject': '  New Topic  '}
    response = client.post('/manage/subjects', data=data)
    assert response.status_code == 201
    assert response.json['subject'] == 'New Topic'
    data['subject'] = 'new topic'
    assert client.post('/manage/subjects', data=data).json['subject'] == 'New Topic'
    from paper_library.catalog.subjects import list_subjects
    from paper_library.catalog import LibraryRepository
    assert list_subjects(LibraryRepository(settings.paths.libraries), 'whitespace') == ['New Topic']
    restarted = create_app(settings, start_jobs=False)
    assert 'New Topic' in restarted.test_client().get('/manage/import').get_data(as_text=True)
    created = upload(client, data['management_token'], subject='New Topic')
    job = app.extensions['job_repository'].get(created.headers['Location'].rsplit('/', 1)[-1])
    assert job.subject == 'New Topic'


def test_subject_creation_validates_name_and_authorization(tmp_path: Path):
    app = create_app(make_settings(tmp_path), start_jobs=False)
    client = app.test_client()
    data = {'library_id': 'whitespace', 'subject': 'Topic'}
    assert client.post('/manage/subjects', data=data).status_code == 403
    data['management_token'] = app.extensions['management_guard'].token
    for name in (' ', 'x' * 121, 'a\nb'):
        data['subject'] = name
        assert client.post('/manage/subjects', data=data).status_code == 400
    data.update(library_id='../outside', subject='Topic')
    assert client.post('/manage/subjects', data=data).status_code == 400


def test_library_creation_persists_and_accepts_subjects_and_uploads(tmp_path: Path):
    settings = make_settings(tmp_path)
    app = create_app(settings, start_jobs=False)
    client = app.test_client()
    token = app.extensions['management_guard'].token
    response = client.post('/manage/libraries', data={
        'management_token': token, 'name': '  强化学习 "RL"  ',
    })
    assert response.status_code == 201
    library = response.json['library']
    assert library['name'] == '强化学习 "RL"'
    repository = app.extensions['library_repository']
    assert repository.load(library['id']).papers == ()
    assert repository.validate_all() == ()
    assert client.post('/manage/subjects', data={
        'management_token': token, 'library_id': library['id'], 'subject': 'Policy',
    }).status_code == 201
    response = upload(client, token, library_id=library['id'], subject='Policy')
    assert response.status_code == 303
    job = app.extensions['job_repository'].get(response.headers['Location'].rsplit('/', 1)[-1])
    assert job.library_id == library['id']
    restarted = create_app(settings, start_jobs=False)
    assert library['id'] in restarted.extensions['library_repository'].discover()


def test_library_creation_rejects_invalid_names_duplicates_and_unauthorized_requests(tmp_path: Path):
    app = create_app(make_settings(tmp_path), start_jobs=False)
    client = app.test_client()
    assert client.post('/manage/libraries', data={'name': 'New'}).status_code == 403
    token = app.extensions['management_guard'].token
    for name in (' ', 'x' * 121, 'a\nb', ' whitespace '):
        assert client.post('/manage/libraries', data={
            'management_token': token, 'name': name,
        }).status_code == 400
    assert set(app.extensions['library_repository'].discover()) == {'whitespace'}
    remote = create_app(make_settings(tmp_path / 'remote', PAPER_LIBRARY_HOST='0.0.0.0'), start_jobs=False)
    assert remote.test_client().post('/manage/libraries', data={
        'management_token': remote.extensions['management_guard'].token, 'name': 'New',
    }).status_code == 403


def test_mutations_require_token_and_remote_opt_in(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path), start_jobs=False)
    client = app.test_client()
    assert upload(client, "wrong").status_code == 403
    assert client.get("/manage/jobs").status_code == 405

    remote = create_app(
        make_settings(tmp_path / "remote", PAPER_LIBRARY_HOST="0.0.0.0"),
        start_jobs=False,
    )
    remote_token = remote.extensions["management_guard"].token
    assert upload(remote.test_client(), remote_token).status_code == 403


def test_job_api_has_allowlisted_fields_and_no_sensitive_paths(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path), start_jobs=False)
    client = app.test_client()
    token = app.extensions["management_guard"].token
    response = upload(client, token)
    job_id = response.headers["Location"].rsplit("/", 1)[-1]

    api = client.get(f"/api/jobs/{job_id}")
    payload = api.get_json()

    assert set(payload) == {
        "id", "filename", "library", "subject", "state", "stage",
        "paper_url", "progress", "activity", "outcomes", "error", "created_at", "updated_at", "title", "library_name",
    }
    serialized = api.get_data(as_text=True)
    assert payload['title'] == payload['filename'] and payload['library_name'] == 'Whitespace'
    assert "/home/" not in serialized
    assert "API_KEY" not in serialized
    assert "source.pdf" not in serialized
    assert api.headers["Cache-Control"] == "no-store"
    assert client.get("/api/jobs/missing").status_code == 404
    recent = client.get("/api/jobs")
    assert recent.status_code == 200
    assert recent.get_json()["jobs"][0]["id"] == job_id
    assert recent.headers["Cache-Control"] == "no-store"


def test_cancel_and_retry_are_post_only(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path), start_jobs=False)
    client = app.test_client()
    token = app.extensions["management_guard"].token
    response = upload(client, token)
    job_id = response.headers["Location"].rsplit("/", 1)[-1]

    assert client.get(f"/manage/jobs/{job_id}/cancel").status_code == 405
    cancelled = client.post(
        f"/manage/jobs/{job_id}/cancel", data={"management_token": token}
    )
    assert cancelled.status_code == 303
    assert app.extensions["job_repository"].get(job_id).state.value == "cancelled"


def test_jobs_persist_across_app_recreation(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    first = create_app(settings, start_jobs=False)
    token = first.extensions["management_guard"].token
    response = upload(first.test_client(), token)
    job_id = response.headers["Location"].rsplit("/", 1)[-1]

    second = create_app(settings, start_jobs=False)

    assert second.test_client().get(f"/manage/jobs/{job_id}").status_code == 200
    assert second.test_client().get(f"/api/jobs/{job_id}").get_json()["id"] == job_id
