import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from paper_library.config import Settings, ProjectPaths
from paper_library.storage.backup import create_backup
import paper_library.storage.remote_backup as remote


def setup_mirror(tmp_path,monkeypatch):
    settings=Settings.load(ProjectPaths.from_root(tmp_path/'project'),{
        'PAPER_BACKUP_REMOTE_DIR':str(tmp_path/'remote'),
        'PAPER_MINERU_REMOTE_PYTHON':sys.executable,'PAPER_BACKUP_KEEP':'1'})
    settings.paths.libraries.mkdir(parents=True)
    (settings.paths.libraries/'article.md').write_text('original')
    backup=create_backup(settings)
    calls=[]
    class Transport:
        def __init__(self,*args):self.target='server'
        def copy(self,source,destination):
            calls.append(str(source));shutil.copy2(source,destination.split(':',1)[1])
    def execute(transport,python,arguments):
        result=subprocess.run([sys.executable,'-c',remote.WORKER,*arguments],capture_output=True,text=True)
        data=json.loads(result.stdout)
        if result.returncode:raise ValueError(data['error'])
        return data
    monkeypatch.setattr(remote,'_remote_call',execute)
    return settings,backup,Transport,calls


def test_remote_archive_verified_reused_and_receipt_recovered(tmp_path,monkeypatch):
    settings,backup,transport,calls=setup_mirror(tmp_path,monkeypatch)
    first=remote.mirror_backup_remote(settings,backup,transport_factory=transport)
    assert first['files']>0 and first['reused'] is False and len(calls)==1
    receipt=tmp_path/'remote'/(backup.name+'.receipt.json')
    assert json.loads(receipt.read_text())['archive_sha256']==first['archive_sha256']
    receipt.unlink()
    second=remote.mirror_backup_remote(settings,backup,transport_factory=transport)
    assert second['reused'] and len(calls)==1 and receipt.exists()
    assert not list((tmp_path/'remote').glob('.upload-*'))


def test_remote_corruption_is_reported_and_never_overwritten(tmp_path,monkeypatch):
    settings,backup,transport,calls=setup_mirror(tmp_path,monkeypatch)
    remote.mirror_backup_remote(settings,backup,transport_factory=transport)
    archive=tmp_path/'remote'/(backup.name+'.tar.gz');archive.write_bytes(b'corrupt')
    with pytest.raises(ValueError):remote.mirror_backup_remote(settings,backup,transport_factory=transport)
    assert archive.read_bytes()==b'corrupt' and len(calls)==1


def test_remote_prunes_only_verified_copies_after_new_copy(tmp_path,monkeypatch):
    settings,backup,transport,calls=setup_mirror(tmp_path,monkeypatch)
    remote.mirror_backup_remote(settings,backup,transport_factory=transport)
    old=tmp_path/'remote'/(backup.name+'.tar.gz')
    (tmp_path/'remote'/'unrelated.txt').write_text('keep')
    (settings.paths.libraries/'article.md').write_text('new')
    latest=create_backup(settings)
    remote.mirror_backup_remote(settings,latest,transport_factory=transport)
    assert not old.exists() and (tmp_path/'remote'/(latest.name+'.tar.gz')).exists()
    assert (tmp_path/'remote'/'unrelated.txt').read_text()=='keep'


def test_remote_rejects_existing_non_backup_directory(tmp_path,monkeypatch):
    settings,backup,transport,calls=setup_mirror(tmp_path,monkeypatch)
    root=tmp_path/'remote';root.mkdir();(root/'important').write_text('keep')
    with pytest.raises(ValueError,match='非空'):
        remote.mirror_backup_remote(settings,backup,transport_factory=transport)
    assert (root/'important').read_text()=='keep' and not calls


def test_remote_transport_failure_keeps_local_backup(tmp_path,monkeypatch):
    settings,backup,transport,calls=setup_mirror(tmp_path,monkeypatch)
    class Failing(transport):
        def copy(self,*args):raise ValueError('SSH unavailable')
    with pytest.raises(ValueError,match='SSH'):
        remote.mirror_backup_remote(settings,backup,transport_factory=Failing)
    assert (backup/'manifest.json').exists()
    assert not list((tmp_path/'remote').glob('*.tar.gz'))
