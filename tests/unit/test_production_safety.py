"""Regression coverage for corruption boundaries, crash recovery and queue pressure."""
import csv
import json
import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from paper_library.catalog import LibraryRepository, PaperPaths
from paper_library.ingest.service import ingest_pdf, refresh_metadata
from paper_library.jobs import JobRepository, NewJob
from paper_library.jobs.models import QueueFull, JobState
from paper_library.storage.publication import publish_files, recover_publications
from test_ingest_service import write_library, SuccessfulRunner


def test_invalid_catalog_stops_import_and_metadata_refresh_before_work(tmp_path, monkeypatch):
    source=tmp_path/'old.pdf';source.write_bytes(b'%PDF-old')
    repo,root=write_library(tmp_path/'libraries',source)
    with (root/'catalog.tsv').open('a') as stream:
        csv.writer(stream,delimiter='\t').writerow(['ws-p0002','2026','','','topic','','hash','','',''])
    original=(root/'catalog.tsv').read_bytes()
    new=tmp_path/'new.pdf';new.write_bytes(b'%PDF-new')
    runner=SuccessfulRunner()
    with pytest.raises(ValueError,match='停止写入'):
        ingest_pdf(repo,'whitespace',new,enrich=False,mineru_runner=runner)
    monkeypatch.setattr('paper_library.metadata.repair.verify_bibliography',lambda *a,**k:pytest.fail('must not call the model or search'))
    with pytest.raises(ValueError,match='停止写入'):
        refresh_metadata(repo,'whitespace','ws-p0001')
    assert runner.calls==0 and (root/'catalog.tsv').read_bytes()==original


def test_new_id_reserves_orphan_paths_and_aliases(tmp_path, monkeypatch):
    source=tmp_path/'old.pdf';source.write_bytes(b'%PDF-old')
    repo,root=write_library(tmp_path/'libraries',source)
    paths=PaperPaths.for_id(root,'ws-p0002');paths.root.mkdir();paths.pdf.write_bytes(b'%PDF-orphan')
    catalog=(root/'catalog.tsv').read_text().replace('2024-01-01\t\n','2024-01-01\tws-p0003\n')
    (root/'catalog.tsv').write_text(catalog)
    new=tmp_path/'new.pdf';new.write_bytes(b'%PDF-new')
    monkeypatch.setattr('paper_library.ingest.paragraphs.repair_file',lambda *a,**k:None)
    result=ingest_pdf(repo,'whitespace',new,enrich=False,mineru_runner=SuccessfulRunner())
    assert result.paper_id=='ws-p0004'
    assert paths.pdf.read_bytes()==b'%PDF-orphan'
    assert repo.load('whitespace').find('ws-p0003').id=='ws-p0001'


def crash_files(root, after):
    library=Path(root);replace=os.replace;count=0
    targets={library/'catalog.tsv',library/'papers'/'p1'/'metadata.json',library/'papers'/'p1'/'article.md'}
    def die(source,target):
        nonlocal count
        replace(source,target)
        if Path(target) in targets:
            count+=1
            if count==after:os._exit(71)
    os.replace=die
    publish_files(library,'p1',{'metadata.json':b'new meta','article.md':b'new text','catalog.tsv':b'new catalog'},lambda:None)


@pytest.mark.parametrize('after',[1,2,3])
def test_metadata_transaction_recovers_process_death_at_each_file(tmp_path,after):
    package=tmp_path/'papers'/'p1';package.mkdir(parents=True)
    originals={tmp_path/'catalog.tsv':b'old catalog',package/'metadata.json':b'old meta',package/'article.md':b'old text'}
    for path,data in originals.items():path.write_bytes(data)
    child=multiprocessing.get_context('spawn').Process(target=crash_files,args=(str(tmp_path),after))
    child.start();child.join(10)
    if child.is_alive():child.terminate();child.join()
    assert child.exitcode==71
    assert list(tmp_path.glob('.transactions/*/journal.json'))
    recover_publications(tmp_path);recover_publications(tmp_path)
    assert all(path.read_bytes()==data for path,data in originals.items())
    assert not list(tmp_path.glob('.transactions/*/journal.json'))


def test_metadata_rollback_failure_keeps_recoverable_journal(tmp_path,monkeypatch):
    package=tmp_path/'papers'/'p1';package.mkdir(parents=True)
    originals={tmp_path/'catalog.tsv':b'old catalog',package/'metadata.json':b'old meta',package/'article.md':b'old text'}
    for path,data in originals.items():path.write_bytes(data)
    replace=os.replace
    def fail(source,target):
        if Path(target)==package/'article.md' or Path(source).name.startswith('restore-'):
            raise OSError('simulated write failure')
        replace(source,target)
    with monkeypatch.context() as patch:
        patch.setattr('paper_library.storage.publication.os.replace',fail)
        with pytest.raises(OSError):publish_files(tmp_path,'p1',{'metadata.json':b'new meta','article.md':b'new text','catalog.tsv':b'new catalog'},lambda:None)
    assert list(tmp_path.glob('.transactions/*/journal.json'))
    recover_publications(tmp_path)
    assert all(path.read_bytes()==data for path,data in originals.items())


def test_concurrent_retries_and_continue_share_the_queue_limit(tmp_path):
    jobs=JobRepository(tmp_path/'jobs.sqlite3',max_pending=1)
    new=NewJob('fake.pdf','library','topic','en',False,False,'fake/source.pdf')
    first=jobs.create(new);jobs.claim_next('test');jobs.fail(first.id,'failed')
    second=jobs.create(new);jobs.claim_next('test');jobs.complete(second.id)
    with jobs._connect() as db:db.execute('UPDATE jobs SET paper_id=? WHERE id=?',('p1',second.id))
    def resume(function,key):
        try:function(key);return True
        except QueueFull:return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(resume,jobs.retry,first.id),pool.submit(resume,jobs.enable_translation,second.id)]
    assert sum(f.result() for f in futures)==1
    assert jobs.state_counts()[JobState.QUEUED]==1
