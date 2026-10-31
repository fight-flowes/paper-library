import io
from pathlib import Path
import tarfile

import pytest

from paper_library.ingest.remote import extract_result, parse_remote, Transport
from paper_library.ingest.runtime import parser_command

REMOTE = {
    'PAPER_MINERU_REMOTE_HOST': '127.0.0.1',
    'PAPER_MINERU_REMOTE_PORT': '12222',
    'PAPER_MINERU_REMOTE_USER': 'remote',
    'PAPER_MINERU_REMOTE_PYTHON': '/remote/env/bin/python',
    'PAPER_MINERU_REMOTE_ROOT': '/remote/parser',
    'PAPER_MINERU_REMOTE_MACHINE_ID': 'f' * 32,
}


def test_local_mode_and_missing_remote_config_never_resolve_local_mineru(tmp_path, monkeypatch):
    local = tmp_path / 'mineru'
    local.write_text('#!/bin/sh\ntouch "' + str(tmp_path / 'executed') + '"\n')
    local.chmod(0o755)
    for config in ({'PAPER_MINERU_EXECUTABLE':str(local)}, {**REMOTE,'PAPER_MINERU_MODE':'local'}):
        with pytest.raises(ValueError, match='禁止|本机'):
            parse_remote(tmp_path / 'test.pdf', tmp_path / 'out', 'en', config)
    assert not (tmp_path / 'executed').exists()


def test_remote_connection_failure_is_an_error_without_local_fallback(tmp_path, monkeypatch):
    commands = []
    def fail(self, arguments, **kwargs):
        commands.append(arguments)
        raise ValueError('SSH unavailable')
    monkeypatch.setattr(Transport, 'run', fail)
    with pytest.raises(ValueError, match='SSH unavailable'):
        parse_remote(tmp_path / 'test.pdf', tmp_path / 'out', 'en', REMOTE)
    assert commands[0][1] == '-c' and 'pinned cicore01' in commands[0][2]
    assert len(commands) == 1, 'Do not invoke any worker on an unverified machine'
    assert not (tmp_path / 'out').exists()


def test_pdf_metadata_only_reads_remote_evidence(tmp_path, monkeypatch):
    import builtins
    import json
    from paper_library.metadata.repair import pdf_frontmatter
    original = builtins.__import__
    def no_pdf_import(name, *args, **kwargs):
        assert name != 'pypdf', 'Local PDF parser must never be imported'
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', no_pdf_import)
    pdf = tmp_path / 'source.pdf'
    pdf.write_bytes(b'%PDF-not-parsed')
    assert pdf_frontmatter(pdf) == ('', '')
    (tmp_path / 'pdf-frontmatter.json').write_text(json.dumps({'title':'Remote title','text':'Remote page'}))
    assert pdf_frontmatter(pdf) == ('Remote title', 'Remote page')


def test_transfer_and_result_extraction(tmp_path, monkeypatch):
    calls = []
    def run(self, arguments, **kwargs):
        calls.append(arguments)
    def copy(self, source, destination):
        if str(source).endswith('result.tar.gz'):
            with tarfile.open(destination, 'w:gz') as archive:
                text = b'# Remote paper\n'
                member = tarfile.TarInfo('source/hybrid/source.md')
                member.size = len(text)
                archive.addfile(member, io.BytesIO(text))
    monkeypatch.setattr(Transport, 'run', run)
    monkeypatch.setattr(Transport, 'copy', copy)
    parse_remote(tmp_path / 'source.pdf', tmp_path / 'out', 'en', REMOTE)
    assert (tmp_path / 'out/source/hybrid/source.md').read_text() == '# Remote paper\n'
    assert calls[0][1] == '-c' and 'pinned cicore01' in calls[0][2]
    assert [call[2] for call in calls[1:]] == ['prepare','parse','cleanup']


@pytest.mark.parametrize('name,kind', [('../escaped.md', tarfile.REGTYPE),('/absolute.md',tarfile.REGTYPE),('link.md',tarfile.SYMTYPE)])
def test_extraction_rejects_paths_and_links(tmp_path, name, kind):
    archive = tmp_path / 'result.tar.gz'
    with tarfile.open(archive,'w:gz') as stream:
        member = tarfile.TarInfo(name)
        member.type = kind
        member.linkname = '/etc/passwd'
        stream.addfile(member, io.BytesIO(b''))
    with pytest.raises(ValueError):
        extract_result(archive, tmp_path / 'out')
    assert not (tmp_path / 'escaped.md').exists()


def test_remote_worker_rejects_wrong_machine_before_any_parser_import(tmp_path, monkeypatch):
    import json
    import sys
    from paper_library.ingest import remote_worker
    (tmp_path / 'deployment.json').write_text(json.dumps({'machine_id':'not-this-host'}))
    monkeypatch.setattr(remote_worker,'ROOT',tmp_path)
    monkeypatch.setattr(sys,'argv',['worker.py','check'])
    with pytest.raises(ValueError,match='restricted'):
        remote_worker.main()


def test_remote_cleanup_does_not_remove_a_running_job(tmp_path, monkeypatch):
    import fcntl
    import json
    import sys
    from paper_library.ingest import remote_worker
    (tmp_path / 'deployment.json').write_text(json.dumps({'machine_id':Path('/etc/machine-id').read_text().strip()}))
    job='a'*32
    directory=tmp_path/'jobs'/job
    directory.mkdir(parents=True)
    monkeypatch.setattr(remote_worker,'ROOT',tmp_path)
    monkeypatch.setattr(sys,'argv',['worker.py','cleanup',job])
    with (directory/'job.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        remote_worker.main()
        assert directory.exists()
    remote_worker.main()
    assert not directory.exists()


def test_disconnected_remote_job_exits_before_starting_gpu_or_pdf_work(tmp_path, monkeypatch):
    import json
    import sys
    from paper_library.ingest import remote_worker
    (tmp_path / 'deployment.json').write_text(json.dumps({'machine_id':Path('/etc/machine-id').read_text().strip()}))
    job='b'*32
    directory=tmp_path/'jobs'/job
    directory.mkdir(parents=True)
    (directory/'source.pdf').write_bytes(b'%PDF-fixture')
    monkeypatch.setattr(remote_worker,'ROOT',tmp_path)
    monkeypatch.setattr(sys,'argv',['worker.py','parse',job])
    class Disconnected:
        def fileno(self): return 0
    monkeypatch.setattr(sys,'stdin',Disconnected())
    monkeypatch.setattr(remote_worker.os,'read',lambda *_: b'')
    monkeypatch.setattr(remote_worker.select,'select',lambda *_: ([0],[],[]))
    def synchronous_watch(self):
        self._target(*self._args)
    monkeypatch.setattr(remote_worker.threading.Thread,'start',synchronous_watch)
    with pytest.raises(ValueError,match='cancelled'):
        remote_worker.main()
    assert not (directory/'output').exists()


def test_worker_exits_cleanly_while_ssh_stdin_is_still_open():
    import subprocess
    import sys
    script = '''from threading import Thread, Event
import time
from paper_library.ingest.remote_worker import watch_connection
Thread(target=watch_connection,args=(Event(),),daemon=True).start()
time.sleep(.1)
print('ready',flush=True)
'''
    process = subprocess.Popen([sys.executable,'-c',script],stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        assert process.stdout.readline().strip() == 'ready'
        # Keep stdin open: this is the state that previously caused a fatal
        # BufferedReader-lock error during worker interpreter shutdown.
        assert process.wait(timeout=5) == 0
        assert 'Fatal Python error' not in process.stderr.read()
    finally:
        process.stdin.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_stop_reaps_detached_job_children_and_preserves_unrelated_processes():
    import subprocess
    import sys
    import signal
    from paper_library.ingest.remote_worker import process_identity, owned_tree, alive, stop, signal_owned
    child_script = 'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print("ready",flush=True); time.sleep(30)'
    parent_script = '''import subprocess,sys,time
child=subprocess.Popen([sys.executable,'-c',sys.argv[1]],start_new_session=True,stdout=subprocess.PIPE,text=True)
assert child.stdout.readline().strip()=='ready'
print(child.pid,flush=True)
time.sleep(30)
'''
    unrelated = subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])
    parent = subprocess.Popen([sys.executable,'-c',parent_script,child_script],
                              start_new_session=True,stdout=subprocess.PIPE,text=True)
    owned = {}
    try:
        child_pid = int(parent.stdout.readline())
        born = process_identity(parent.pid)[1]
        owned = owned_tree(parent.pid,born)
        assert child_pid in owned
        assert unrelated.pid not in owned
        stop(parent,owned,born)
        assert not alive(child_pid,owned[child_pid])
        assert unrelated.poll() is None
    finally:
        for pid, started in owned.items():
            signal_owned(pid,started,signal.SIGKILL)
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=5)
        unrelated.terminate()
        unrelated.wait(timeout=5)


def test_worker_stops_when_the_client_heartbeat_disappears(monkeypatch):
    from threading import Event
    from paper_library.ingest import remote_worker
    lost = Event()
    times = iter([0, 46])
    monkeypatch.setattr(remote_worker.time,'monotonic',lambda:next(times))
    monkeypatch.setattr(remote_worker.select,'select',lambda *_:([],[],[]))
    remote_worker.watch_connection(lost)
    assert lost.is_set()
