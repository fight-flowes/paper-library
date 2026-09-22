import logging
import json
from types import SimpleNamespace
from pathlib import Path
from paper_library.operations.logging import SafeFormatter
from paper_library.operations.systemd import write_units


def test_logs_do_not_include_credentials():
    record=logging.LogRecord('paper_library',logging.ERROR,'',1,'failed Authorization: Bearer SECRET',(),None)
    value=SafeFormatter().format(record)
    assert 'SECRET' not in value
    assert json.loads(value)['level']=='ERROR'


def test_units_use_current_interpreter_and_bounded_restarts(tmp_path):
    settings=SimpleNamespace(paths=SimpleNamespace(root=tmp_path/'project'))
    root=write_units(settings,tmp_path/'units')
    assert 'Restart=on-failure' in (root/'paper-library.service').read_text()
    assert '--scheduled' in (root/'paper-library-backup.service').read_text()
    assert 'Persistent=true' in (root/'paper-library-backup.timer').read_text()


def test_json_and_basic_credentials_are_redacted():
    for message in ('{"api_key": "test-secret"}','Authorization: Basic test-secret','password=test-secret'):
        record=logging.LogRecord('paper_library',logging.ERROR,'',1,message,(),None)
        assert 'test-secret' not in SafeFormatter().format(record)
