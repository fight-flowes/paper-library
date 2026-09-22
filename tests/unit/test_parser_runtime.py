from dataclasses import replace
from pathlib import Path
import time
import pytest
from paper_library.config import ProjectPaths,Settings
from paper_library.ingest.runtime import parser_command
from paper_library.ingest.service import MinerURunner


def test_parser_path_and_total_deadline(tmp_path):
    executable=tmp_path/'mineru'
    executable.write_text('#!/bin/sh\necho starting\nsleep 30\n')
    executable.chmod(0o755)
    settings=Settings.load(ProjectPaths.from_root(tmp_path),{'PAPER_MINERU_EXECUTABLE':str(executable)})
    assert parser_command(settings.values)==str(executable)
    settings=replace(settings,values={**settings.values,'PAPER_MINERU_TIMEOUT_SECONDS':'.15'})
    started=time.monotonic();runner=MinerURunner(settings)
    with pytest.raises(ValueError,match='超时'):runner.run(tmp_path/'source.pdf',tmp_path/'out','en')
    assert time.monotonic()-started<3
    assert 'starting' in runner.last_output
