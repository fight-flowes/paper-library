from dataclasses import replace
from pathlib import Path
import time
import pytest
from paper_library.config import ProjectPaths,Settings
from paper_library.ingest.runtime import parser_command
from paper_library.ingest.service import MinerURunner
import subprocess
import shutil


REMOTE = {
    "PAPER_MINERU_REMOTE_HOST": "127.0.0.1",
    "PAPER_MINERU_REMOTE_PORT": "12222",
    "PAPER_MINERU_REMOTE_USER": "test",
    "PAPER_MINERU_REMOTE_PYTHON": "/remote/env/bin/python",
    "PAPER_MINERU_REMOTE_ROOT": "/remote/parser",
}


def simulate_remote_worker(monkeypatch, executable):
    original = subprocess.Popen
    def launch(command, **kwargs):
        assert command[1:3] == ["-m", "paper_library.ingest.remote"]
        return original([str(executable)], **kwargs)
    monkeypatch.setattr(subprocess, "Popen", launch)



def test_parser_path_and_total_deadline(tmp_path, monkeypatch):
    executable=tmp_path/'mineru'
    executable.write_text('#!/bin/sh\necho starting\nsleep 30\n')
    executable.chmod(0o755)
    simulate_remote_worker(monkeypatch, executable)
    settings=Settings.load(ProjectPaths.from_root(tmp_path),{**REMOTE,'PAPER_MINERU_EXECUTABLE':str(executable)})
    assert parser_command(settings.values)==shutil.which("ssh")
    settings=replace(settings,values={**settings.values,'PAPER_MINERU_TIMEOUT_SECONDS':'.15'})
    started=time.monotonic();runner=MinerURunner(settings)
    with pytest.raises(ValueError,match='超时'):runner.run(tmp_path/'source.pdf',tmp_path/'out','en')
    assert time.monotonic()-started<3
    assert 'starting' in runner.last_output


def test_parser_can_disable_idle_deadline_and_receives_resource_settings(tmp_path, monkeypatch):
    executable=tmp_path/'mineru'
    executable.write_text(
        '#!/bin/sh\n'
        'echo "window=$MINERU_PROCESSING_WINDOW_SIZE batch=$MINERU_HYBRID_BATCH_RATIO"\n'
        'sleep .2\n'
        'echo finished\n'
    )
    executable.chmod(0o755)
    simulate_remote_worker(monkeypatch, executable)
    settings=Settings.load(ProjectPaths.from_root(tmp_path),{
        **REMOTE, 'PAPER_MINERU_EXECUTABLE':str(executable),
        'PAPER_MINERU_PROCESSING_WINDOW_SIZE':'8',
        'PAPER_MINERU_HYBRID_BATCH_RATIO':'1',
        'PAPER_MINERU_TIMEOUT_SECONDS':'2',
        'PAPER_MINERU_IDLE_TIMEOUT_SECONDS':'0',
    })

    runner=MinerURunner(settings)
    runner.run(tmp_path/'source.pdf',tmp_path/'out','en')

    assert 'window=8 batch=1' in runner.last_output
    assert 'finished' in runner.last_output


def test_parser_persists_diagnostic_log(tmp_path, monkeypatch):
    executable=tmp_path/'mineru'
    executable.write_text('#!/bin/sh\necho starting\necho completed\n')
    executable.chmod(0o755)
    simulate_remote_worker(monkeypatch, executable)
    settings=Settings.load(ProjectPaths.from_root(tmp_path),{
        **REMOTE, 'PAPER_MINERU_EXECUTABLE':str(executable),
        'PAPER_MINERU_LOG_KEEP':'2',
    })

    runner=MinerURunner(settings)
    runner.run(tmp_path/'source.pdf',tmp_path/'out','en')

    assert runner.last_log is not None
    assert runner.last_log.parent == settings.paths.state/'logs'/'mineru'
    assert runner.last_log.read_text().splitlines() == ['starting','completed']


def test_settings_reject_invalid_mineru_resource_limits(tmp_path):
    paths=ProjectPaths.from_root(tmp_path)
    with pytest.raises(ValueError,match='PAPER_MINERU_PROCESSING_WINDOW_SIZE'):
        Settings.load(paths,{'PAPER_MINERU_PROCESSING_WINDOW_SIZE':'0'})
    with pytest.raises(ValueError,match='PAPER_MINERU_HYBRID_BATCH_RATIO'):
        Settings.load(paths,{'PAPER_MINERU_HYBRID_BATCH_RATIO':'0'})
