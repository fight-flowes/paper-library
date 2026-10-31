import json
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

from paper_library.config import Settings, ProjectPaths
from paper_library.ingest.remote import identity_arguments
from paper_library.operations import connections
from paper_library.preferences import PreferenceError


def settings(tmp_path):
    return Settings.load(ProjectPaths.from_root(tmp_path), {
        'PAPER_TRANSLATION_PROFILE': 'model', 'LLM_TEST_MODEL': 'model',
        'LLM_TEST_BASE_URL': 'http://fixture/v1', 'LLM_TEST_API_KEY': 'secret',
        'LLM_TEST_REQUEST_STYLE': 'qwen',
        'PAPER_MINERU_REMOTE_HOST': '127.0.0.1', 'PAPER_MINERU_REMOTE_USER': 'fixture',
        'PAPER_MINERU_REMOTE_PYTHON': '/remote/python', 'PAPER_MINERU_REMOTE_ROOT': '/remote/parser',
        'PAPER_MINERU_REMOTE_MACHINE_ID': 'f' * 32,
    })


@pytest.mark.parametrize('code,body,error', [
    (200, {'choices': [{'message': {'content': 'OK'}}]}, ''),
    (401, {'secret': 'must not be echoed'}, '认证失败'),
    (403, {}, '认证失败'), (503, {}, 'HTTP 503'),
    (200, {'choices': []}, '无法使用'),
    (200, {'choices': [{'message': {'content': None}}]}, '无法使用'),
])
def test_model_probe_checks_real_response_and_never_exposes_error_body(tmp_path, monkeypatch, code, body, error):
    real = httpx.Client
    captured = []
    def handle(request):
        captured.append(json.loads(request.content))
        assert request.headers['Authorization'] == 'Bearer secret'
        return httpx.Response(code, json=body)
    monkeypatch.setattr(connections.httpx, 'Client', lambda **kwargs: real(transport=httpx.MockTransport(handle), **kwargs))
    if error:
        with pytest.raises(PreferenceError, match=error) as exc: connections.test_model(settings(tmp_path))
        assert 'must not be echoed' not in str(exc.value)
    else:
        assert connections.test_model(settings(tmp_path))['model'] == 'model'
    assert captured[0]['chat_template_kwargs'] == {'enable_thinking': False}
    assert captured[0]['temperature'] == .1
    assert captured[0]['max_tokens'] == settings(tmp_path).translation().max_tokens


def test_machine_identity_rejects_local_and_unpinned_targets_before_pdf_work(tmp_path):
    current = settings(tmp_path)
    local = Path('/etc/machine-id').read_text().strip()
    for value in ('', 'bad-id', local):
        with pytest.raises(ValueError, match='本机'):
            identity_arguments({**current.values, 'PAPER_MINERU_REMOTE_MACHINE_ID': value})
    marker = tmp_path / 'executed'
    worker = tmp_path / 'worker.py'; worker.write_text(f'from pathlib import Path;Path({str(marker)!r}).touch()')
    values = {**current.values, 'PAPER_MINERU_REMOTE_ROOT': str(tmp_path)}
    arguments = identity_arguments(values, check_worker=True)
    result = subprocess.run([sys.executable, *arguments[1:]], capture_output=True)
    assert result.returncode != 0 and b'pinned cicore01' in result.stderr
    assert not marker.exists(), 'An unverified node must never execute the worker'


def test_remote_probe_uses_fresh_authentication_and_checks_machine_and_gpu(tmp_path, monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps({'cuda': True, 'gpu': 'GPU', 'mineru_version': '3.0.9'}).encode(), b'')
    monkeypatch.setattr(connections.subprocess, 'run', run)
    assert connections.test_remote(settings(tmp_path))['gpu'] == 'GPU'
    assert calls[0][1:5] == ['-o', 'ControlMaster=no', '-o', 'ControlPath=none']
    assert 'machine-id' in calls[0][-1] and 'runpy' in calls[0][-1]
