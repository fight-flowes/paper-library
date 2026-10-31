"""Corrupt auxiliary files must report degradation without losing user data."""
from dataclasses import replace
from pathlib import Path
import json

import pytest

from test_reader import make_settings
from paper_library.catalog import CATALOG_COLUMNS, PaperPaths
from paper_library.web.app import create_app
from paper_library.storage.backup import record_backup_status, verify_backup


@pytest.mark.parametrize('payload',[None,[],{'remote_mirror':None},{'remote_mirror':[]},
                                    {'status':[]},{'status':'ok','remote_mirror':{'status':'ok','verified_at':{}}}])
def test_bad_backup_status_is_readable_and_can_be_repaired(tmp_path,payload):
    settings=make_settings(tmp_path)
    settings=replace(settings,values={**settings.values,'PAPER_BACKUP_REMOTE_DIR':'/remote/backup'})
    app=create_app(settings,start_jobs=False);client=app.test_client()
    path=settings.paths.state/'backup-status.json';path.write_text(json.dumps(payload));original=path.read_bytes()
    assert client.get('/manage/status').status_code==200
    assert client.get('/health/ready').status_code==200
    assert path.read_bytes()==original
    record_backup_status(settings,status='ok',remote_mirror={'status':'ok'})
    repaired=json.loads(path.read_text())
    assert repaired['status']=='ok' and repaired['remote_mirror']['status']=='ok'
    assert 'error' not in repaired


@pytest.mark.parametrize('payload',[
    {'state':'prepared','paper':'ex-p0001'},
    {'state':[]},
    {'state':'prepared','kind':[],'had_package':True},
    {'state':'prepared','paper':[],'staging':'stage','had_package':True},
    {'state':'prepared','kind':'files','paper':'ex-p0001','files':[['bad']]},
])
def test_bad_journal_is_retained_and_other_libraries_remain_available(tmp_path,payload):
    settings=make_settings(tmp_path)
    healthy=settings.paths.libraries/'healthy';healthy.mkdir()
    (healthy/'library.toml').write_text('schema_version=1\nid="healthy"\nname="Healthy"\npaper_id_prefix="h"\n')
    (healthy/'catalog.tsv').write_text('\t'.join(CATALOG_COLUMNS)+'\n')
    journal=settings.paths.libraries/'example/.transactions/invalid/journal.json'
    journal.parent.mkdir(parents=True);journal.write_text(json.dumps(payload));original=journal.read_bytes()
    app=create_app(settings,start_jobs=False);client=app.test_client()
    assert journal.read_bytes()==original
    assert client.get('/').status_code==200
    assert client.get('/library/healthy/').status_code==200
    assert client.get('/library/example/paper/ex-p0001').status_code==503
    assert client.get('/health/ready').status_code==503


@pytest.mark.parametrize('bibliography',[
    ['bad'],
    {'venue_type':['conference']},
    {'canonical_title':{'text':'bad'}},
    {'authors':['Name',3]},
])
def test_bad_bibliography_keeps_original_paper_readable(tmp_path,bibliography):
    settings=make_settings(tmp_path)
    path=PaperPaths.for_id(settings.paths.libraries/'example','ex-p0001').metadata
    path.write_text(json.dumps({'bibliography':bibliography}));original=path.read_bytes()
    app=create_app(settings,start_jobs=False);client=app.test_client()
    response=client.get('/library/example/paper/ex-p0001')
    assert response.status_code==200
    assert 'English paragraph' in response.get_data(as_text=True)
    assert '格式无效' in response.get_data(as_text=True)
    assert client.get('/health/ready').status_code==503
    assert path.read_bytes()==original


@pytest.mark.parametrize('created',[None,'bad','2026-10-28T12:00:00'])
def test_backup_requires_a_valid_creation_time(tmp_path,created):
    root=tmp_path/'backup';root.mkdir()
    (root/'manifest.json').write_text(json.dumps({'schema':1,'created_at':created,'files':{}}))
    with pytest.raises(ValueError,match='时间'):verify_backup(root)


def test_bad_backup_manifest_time_does_not_break_status_page(tmp_path):
    settings=make_settings(tmp_path)
    root=settings.paths.root/'.backups/bad';root.mkdir(parents=True)
    (root/'manifest.json').write_text(json.dumps({'schema':1,'created_at':{'bad':'time'},'files':{}}))
    app=create_app(settings,start_jobs=False)
    assert app.test_client().get('/manage/status').status_code==200
