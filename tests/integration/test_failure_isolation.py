from pathlib import Path
import json
import sqlite3

from test_reader import make_settings
from paper_library.web.app import create_app
from paper_library.catalog import PaperPaths
from paper_library.operations.health import check


def test_bad_library_is_visible_but_does_not_block_healthy_library(tmp_path):
    settings=make_settings(tmp_path)
    broken=settings.paths.libraries/'broken';broken.mkdir();(broken/'library.toml').write_text('invalid = [')
    app=create_app(settings,start_jobs=False);client=app.test_client()
    assert client.get('/library/example/paper/ex-p0001').status_code==200
    assert client.get('/manage/import?library=example').status_code==200
    assert client.get('/search?q=English').status_code==200
    assert 'English' in client.get('/search?q=English').get_data(as_text=True)
    assert client.get('/reading').status_code==200
    assert 'broken' in client.get('/').get_data(as_text=True)
    assert client.get('/health/ready').status_code==503
    assert not check(settings)['reading_ready']


def test_unreadable_library_is_isolated_before_config_open(tmp_path,monkeypatch):
    settings=make_settings(tmp_path)
    broken=settings.paths.libraries/'broken';broken.mkdir()
    original=Path.is_file
    def unreadable(path):
        if path==broken/'library.toml':raise PermissionError('unreadable library')
        return original(path)
    monkeypatch.setattr(Path,'is_file',unreadable)
    app=create_app(settings,start_jobs=False);client=app.test_client()
    assert client.get('/').status_code==200
    assert client.get('/library/example/paper/ex-p0001').status_code==200
    assert client.get('/health/ready').status_code==503


def test_corrupt_search_index_is_preserved_and_rebuilt_on_startup(tmp_path):
    settings=make_settings(tmp_path);settings.paths.state.mkdir(parents=True)
    path=settings.paths.state/'search.sqlite3';original=b'not a sqlite database';path.write_bytes(original)
    app=create_app(settings,start_jobs=False);client=app.test_client()
    assert client.get('/library/example/paper/ex-p0001').status_code==200
    assert 'English' in client.get('/search?q=English').get_data(as_text=True)
    quarantined=list((settings.paths.state/'search-quarantine').glob('*/search.sqlite3'))
    assert len(quarantined)==1 and quarantined[0].read_bytes()==original
    assert app.extensions['search_index'].enabled


def test_unwritable_search_degrades_without_removing_existing_index(tmp_path,monkeypatch):
    import paper_library.personal.search as search
    settings=make_settings(tmp_path)
    def unavailable(self):raise sqlite3.OperationalError('readonly database')
    monkeypatch.setattr(search.SearchIndex,'_initialize',unavailable)
    app=create_app(settings,start_jobs=False);client=app.test_client()
    assert client.get('/library/example/paper/ex-p0001').status_code==200
    assert '检索索引暂不可用' in client.get('/search?q=English').get_data(as_text=True)
    assert not list((settings.paths.state/'search-quarantine').glob('*'))


def test_health_agrees_with_ops_for_missing_body_and_invalid_catalog(tmp_path):
    settings=make_settings(tmp_path);app=create_app(settings,start_jobs=False);client=app.test_client()
    assert client.get('/health/ready').status_code==200
    path=PaperPaths.for_id(settings.paths.libraries/'example','ex-p0001').markdown
    path.unlink()
    assert client.get('/health/ready').status_code==503
    assert not check(settings)['reading_ready']
    path.write_text('# Restored\n\nReadable text.')
    assert client.get('/health/ready').status_code==200
    catalog=settings.paths.libraries/'example'/'catalog.tsv'
    with catalog.open('a') as stream:stream.write('invalid\trow\n')
    assert client.get('/health/ready').status_code==503
    assert not check(settings)['reading_ready']


def test_loopback_reader_rejects_untrusted_host(tmp_path):
    app=create_app(make_settings(tmp_path),start_jobs=False);client=app.test_client()
    assert client.get('/manage/import',headers={'Host':'untrusted.invalid'}).status_code==400
    assert client.get('/manage/import',headers={'Host':'127.0.0.1:18020'}).status_code==200


def test_full_queue_retry_is_a_recoverable_http_response(tmp_path):
    from paper_library.jobs import NewJob
    app=create_app(make_settings(tmp_path),start_jobs=False);client=app.test_client()
    jobs=app.extensions['job_repository'];jobs.max_pending=1
    new=NewJob('fake.pdf','example','topic','en',False,False,'fake/source.pdf')
    first=jobs.create(new);jobs.claim_next('test');jobs.fail(first.id,'failure')
    jobs.create(new)
    token=app.extensions['management_guard'].token
    response=client.post('/manage/jobs/'+first.id+'/retry',data={'management_token':token})
    assert response.status_code==429 and response.headers['Retry-After']=='60'
    assert '任务队列已满' in response.get_data(as_text=True)
    assert jobs.get(first.id).state.value=='failed'
