from pathlib import Path
import json
import sqlite3
import pytest
from paper_library.storage.backup import create_backup, verify_backup, restore_backup
from paper_library.storage.history import checkpoint, revisions
from paper_library.config import ProjectPaths, Settings


def settings_at(root):
    paths=ProjectPaths.from_root(root)
    return Settings.load(paths, {'PAPER_TRANSLATION_CACHE':str(root/'cache.sqlite3')})


def test_history_retains_exact_original_and_deduplicates(tmp_path):
    paper=tmp_path/'paper';paper.mkdir(); path=paper/'article.md';path.write_bytes(b'# Original\r\n\r\nText.\r\n')
    first=checkpoint(paper,'before');checkpoint(paper,'same')
    path.write_text('# Changed');checkpoint(paper,'after')
    rows=revisions(paper)
    assert len(rows)==2
    assert (paper/'.history'/first/'article.md').read_bytes()==b'# Original\r\n\r\nText.\r\n'


def test_backup_restore_validates_and_excludes_secrets(tmp_path):
    settings=settings_at(tmp_path/'project');settings.paths.libraries.mkdir(parents=True)
    (settings.paths.libraries/'paper.md').write_text('中文 original')
    settings.paths.state.mkdir(); (settings.paths.state/'management.token').write_text('secret')
    c=sqlite3.connect(settings.paths.state/'personal.sqlite3');c.execute('create table notes(body text)');c.execute("insert into notes values('note')");c.commit();c.close()
    backup=create_backup(settings,tmp_path/'backups')
    assert verify_backup(backup)['files']
    assert not list(backup.rglob('management.token'))
    target=tmp_path/'restored';restore_backup(backup,target)
    assert (target/'libraries'/'paper.md').read_text()=='中文 original'
    with sqlite3.connect(target/'.paper-library-dev'/'personal.sqlite3') as c: assert c.execute('select body from notes').fetchone()[0]=='note'
    with pytest.raises(ValueError):restore_backup(backup,target)
    (backup/'libraries'/'paper.md').write_text('tampered')
    with pytest.raises(ValueError):verify_backup(backup)


def test_backup_refuses_active_writers_and_restore_pauses_jobs(tmp_path):
    from paper_library.storage.locking import gate
    from paper_library.jobs import JobRepository,NewJob
    settings=settings_at(tmp_path/'project');settings.paths.libraries.mkdir(parents=True)
    repo=JobRepository(settings.jobs_database)
    job=repo.create(NewJob('paper.pdf','lib','topic','en',True,True,'x/source.pdf'))
    with gate(settings.paths.root):
        with pytest.raises(ValueError):create_backup(settings,tmp_path/'backups')
    backup=create_backup(settings,tmp_path/'backups');target=tmp_path/'restored'
    restore_backup(backup,target)
    restored=JobRepository(target/'.paper-library-dev'/'jobs.sqlite3')
    assert restored.get(job.id).state.value=='failed'
    assert '手动' in restored.get(job.id).error


def test_restore_rejects_path_traversal(tmp_path):
    root=tmp_path/'backup';root.mkdir()
    (root/'manifest.json').write_text(json.dumps({'schema':1,'files':{'libraries/../../outside':{'sha256':'x','size':1}}}))
    with pytest.raises(ValueError):restore_backup(root,tmp_path/'target')
    assert not (tmp_path/'target').exists()


def test_paper_lock_survives_package_replacement(tmp_path):
    from paper_library.storage.locking import paper_lock
    root=tmp_path/'paper';root.mkdir()
    with paper_lock(root):
        root.rename(tmp_path/'old');root.mkdir()
        with pytest.raises(ValueError):
            with paper_lock(root):pass


def test_scheduled_backup_verifies_before_skipping(tmp_path,monkeypatch):
    from paper_library.cli import ops
    settings=settings_at(tmp_path/'project');settings.paths.libraries.mkdir(parents=True)
    (settings.paths.libraries/'article.md').write_text('Original')
    monkeypatch.setattr(ops,'default_settings',lambda:settings)
    destination=tmp_path/'backups';args=['backup','--scheduled','--directory',str(destination)]
    assert ops.main(args)==0
    assert ops.main(args)==0
    assert len(list(destination.glob('*/manifest.json')))==1
    backup=next(destination.glob('*/manifest.json')).parent
    (backup/'libraries'/'article.md').write_text('Corrupt')
    assert ops.main(args)==0
    assert len(list(destination.glob('*/manifest.json')))==2


def test_version_restores_pdf_images_and_rejects_corruption(tmp_path):
    from paper_library.storage.history import restore_revision
    paper=tmp_path/'paper';paper.mkdir();(paper/'images').mkdir()
    (paper/'article.md').write_text('![image](images/figure.png)')
    (paper/'source.pdf').write_bytes(b'%PDF-example')
    (paper/'images'/'figure.png').write_bytes(b'original image')
    version=checkpoint(paper,'complete')
    (paper/'images'/'figure.png').write_bytes(b'changed image')
    target=restore_revision(paper,version,tmp_path/'restored')
    assert (target/'images'/'figure.png').read_bytes()==b'original image'
    assert (target/'source.pdf').read_bytes()==b'%PDF-example'
    (paper/'.history'/version/'source.pdf').write_bytes(b'corrupt')
    with pytest.raises(ValueError):restore_revision(paper,version,tmp_path/'bad')
    assert not (tmp_path/'bad').exists()


def test_backup_mirror_is_verified(tmp_path):
    from paper_library.storage.backup import mirror_backup
    settings=settings_at(tmp_path/'project');settings.paths.libraries.mkdir(parents=True)
    (settings.paths.libraries/'paper.md').write_text('Original')
    original=create_backup(settings,tmp_path/'primary')
    copy=mirror_backup(original,tmp_path/'secondary')
    assert verify_backup(copy)==verify_backup(original)
    (copy/'libraries'/'paper.md').write_text('Broken')
    with pytest.raises(ValueError):mirror_backup(original,tmp_path/'secondary')


def test_pending_maintenance_blocks_new_writers_until_backup_finishes(tmp_path):
    from threading import Thread,Event
    import time
    from paper_library.storage.locking import gate
    entered=Event();release=Event();writer_entered=Event()
    def maintenance():
        with gate(tmp_path,exclusive=True,timeout=3):
            entered.set();release.wait(3)
    def writer():
        with gate(tmp_path):writer_entered.set()
    thread=Thread(target=maintenance);later=Thread(target=writer)
    try:
        with gate(tmp_path):
            thread.start()
            for _ in range(100):
                try:
                    with gate(tmp_path,blocking=False):pass
                except ValueError:break
                time.sleep(.01)
            else:raise AssertionError('maintenance never acquired intent')
            later.start()
            assert not writer_entered.wait(.1)
        assert entered.wait(1)
        assert not writer_entered.is_set()
        release.set()
        assert writer_entered.wait(1)
    finally:
        release.set();thread.join(4)
        if later.ident:later.join(4)
