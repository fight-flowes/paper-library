from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path

from bs4 import BeautifulSoup
import httpx
import pytest

from test_management_routes import make_settings
from paper_library.config import Settings
from paper_library.preferences import (PreferenceConflict, PreferenceError, RuntimePreferences,
                                       read_preferences, validate_values)
from paper_library.operations import connections
from paper_library.translation.service import TranslationConfig, TranslationService
from paper_library.web.app import create_app
from paper_library.web.markdown import render_translatable_markdown


@pytest.fixture
def configured(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    settings.paths.dotenv.write_text(
        'PAPER_TRANSLATION_PROFILE=original\nLLM_ORIGINAL_MODEL=original\n'
        'LLM_ORIGINAL_BASE_URL=http://original/v1\nLLM_ORIGINAL_API_KEY=original-secret\n'
        'LLM_ORIGINAL_REQUEST_STYLE=qwen\n'
        'PAPER_MINERU_REMOTE_HOST=127.0.0.1\nPAPER_MINERU_REMOTE_PORT=12222\n'
        'PAPER_MINERU_REMOTE_USER=remote\nPAPER_MINERU_REMOTE_PASSWORD=ssh-secret\n'
        'PAPER_MINERU_REMOTE_PYTHON=/remote/python\nPAPER_MINERU_REMOTE_ROOT=/remote/parser\n')
    settings = Settings.load(settings.paths, {})
    monkeypatch.setattr(connections, 'test_model', lambda settings: {'message': '模型调用成功', 'model': settings.translation().model, 'seconds': .1})
    monkeypatch.setattr(connections, 'test_remote', lambda settings: {'message': 'cicore01 解析环境可用', 'gpu': 'fixture GPU', 'seconds': .1})
    app = create_app(settings, start_jobs=False)
    return settings, app, app.test_client()


def submit(app, client, group, **values):
    runtime = app.extensions['runtime_preferences']
    data = {'management_token': app.extensions['management_guard'].token,
            'revision': read_preferences(runtime.initial.paths.state)['revision'], 'group': group, **values}
    return client.post('/manage/settings', data=data, headers={'Accept': 'application/json'})


def test_settings_hide_secrets_preserve_dotenv_and_reload_cli(configured):
    settings, app, client = configured
    before = settings.paths.dotenv.read_bytes()
    page = client.get('/manage/settings')
    assert page.status_code == 200
    assert page.headers['Cache-Control'] == 'no-store'
    assert b'original-secret' not in page.data and b'ssh-secret' not in page.data
    html = BeautifulSoup(page.data, 'html.parser')
    assert all(not input.get('value') for input in html.select('input[type=password]'))
    response = submit(app, client, 'model', model='new-model', base_url='http://new/v1', api_key='new-secret',
                      concurrency='4', request_style='openai')
    assert response.status_code == 200, response.json
    assert 'new-secret' not in response.get_data(as_text=True)
    assert settings.paths.dotenv.read_bytes() == before
    path = settings.paths.state / 'settings.json'
    assert path.stat().st_mode & 0o777 == 0o600
    loaded = Settings.load(settings.paths, {})
    assert loaded.translation().model == 'new-model'
    assert loaded.translation().api_key == 'new-secret'
    assert loaded.translation().concurrency == 4
    assert loaded.base_values['PAPER_MINERU_REMOTE_PASSWORD'] == 'ssh-secret'
    assert submit(app, client, 'model', api_key='').status_code == 200
    assert app.extensions['runtime_preferences'].snapshot().translation().api_key == 'new-secret'
    assert submit(app, client, 'model', api_key_clear='1').status_code == 200
    assert app.extensions['runtime_preferences'].snapshot().translation().api_key == ''


def test_running_workflow_keeps_complete_snapshot_after_settings_change(configured):
    settings, app, client = configured
    runner = app.extensions['job_runner']
    first = runner.workflow_factory()
    assert submit(app, client, 'model', model='next', base_url='http://next/v1').status_code == 200
    assert submit(app, client, 'remote', host='new-tunnel', port='10022', authentication='password').status_code == 200
    second = runner.workflow_factory()
    assert first.translation_service.config.model == 'original'
    assert first.ingest.keywords['repair_settings'].values['PAPER_MINERU_REMOTE_HOST'] == '127.0.0.1'
    assert first.paragraph_repairer.keywords['settings'].translation().model == 'original'
    assert first.metadata_refresher.keywords['settings'].translation().model == 'original'
    assert second.translation_service.config.model == 'next'
    assert second.ingest.keywords['repair_settings'].values['PAPER_MINERU_REMOTE_HOST'] == 'new-tunnel'
    assert second.configuration['revision'] != first.configuration['revision']
    reloaded = Settings.load(settings.paths, {})
    assert reloaded.values['PAPER_MINERU_REMOTE_HOST'] == 'new-tunnel'
    assert reloaded.base_values['PAPER_MINERU_REMOTE_HOST'] == '127.0.0.1'


def test_import_defaults_and_failed_form_values(configured):
    _settings, app, client = configured
    assert submit(app, client, 'import', library='whitespace', language='ch', enrich='0', auto_translate='0').status_code == 200
    page = BeautifulSoup(client.get('/manage/import').data, 'html.parser')
    assert page.select_one('#import-library option[selected]')['value'] == 'whitespace'
    assert page.select_one('select[name=language] option[selected]')['value'] == 'ch'
    assert not page.select_one('input[name=enrich]').has_attr('checked')
    assert not page.select_one('input[name=auto_translate]').has_attr('checked')
    response = submit(app, client, 'import', library='does-not-exist')
    assert response.status_code == 400
    token = app.extensions['management_guard'].token
    error_page = client.post('/manage/jobs', data={'management_token': token, 'library_id': 'whitespace', 'language': 'en'})
    assert error_page.status_code == 400
    error_html = BeautifulSoup(error_page.data, 'html.parser')
    assert error_html.select_one('select[name=language] option[selected]')['value'] == 'en'


def test_saved_translations_from_multiple_models_survive_switch_and_restart(configured):
    settings, app, client = configured
    settings.paths.cache.parent.mkdir(parents=True, exist_ok=True)
    # This fixture uses plain Markdown and a fake model response, never a PDF parser.
    settings = replace(settings, paths=replace(settings.paths, cache=settings.paths.state / 'translations.sqlite3'))
    app = create_app(settings, start_jobs=False); client = app.test_client()
    source = 'An English paragraph.\n\nAnother paragraph.'
    segments = render_translatable_markdown(source, '/asset').segments
    version = hashlib.sha256(source.encode()).hexdigest()
    identity = 'whitespace/ws-p0001'
    def generate(config, segment, text):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': text}}]}))
        service = TranslationService(TranslationConfig.from_settings(config.translation()), settings.paths.cache, transport=transport)
        service.translate(identity, version, segment)
    generate(app.extensions['runtime_preferences'].snapshot(), segments[0], '第一个模型的译文。')
    assert submit(app, client, 'model', model='second', base_url='http://second/v1').status_code == 200
    generate(app.extensions['runtime_preferences'].snapshot(), segments[1], '第二个模型的译文。')
    assert submit(app, client, 'model', model='third', base_url='http://third/v1').status_code == 200
    for reader in (app, create_app(Settings.load(settings.paths, {}), start_jobs=False)):
        # Settings.load normally derives its cache path; this fixture pins it explicitly.
        reader.extensions['runtime_preferences'].initial = replace(reader.extensions['runtime_preferences'].initial,
                                                                   paths=settings.paths)
        records = reader.extensions['translation_service'].cached_many_records(identity, version, segments)
        assert [records[s.id].model for s in segments] == ['original', 'second']
        assert records[segments[0].id].text == '第一个模型的译文。'
    assert not (settings.paths.state / 'settings.json').read_text().count('original-secret')


def test_authorization_origin_revision_conflicts_and_validation(configured):
    settings, app, client = configured
    token = app.extensions['management_guard'].token
    assert client.post('/manage/settings', data={'group': 'import'}).status_code == 403
    for headers in ({'Origin': 'https://other.example'}, {'Sec-Fetch-Site': 'cross-site'}):
        assert client.post('/manage/settings', data={'management_token': token}, headers=headers).status_code == 403
    original = read_preferences(settings.paths.state)['revision']
    assert submit(app, client, 'import', language='ch').status_code == 200
    stale = client.post('/manage/settings', data={'management_token': token, 'revision': original, 'group': 'import', 'language': 'en'})
    assert stale.status_code == 409
    for values in ({'concurrency': 'NaN'}, {'concurrency': '0'}, {'timeout': 'inf'}, {'temperature': 'nan'},
                   {'base_url': 'http://user:password@model/v1'}, {'request_style': 'arbitrary'}):
        before = (settings.paths.state / 'settings.json').read_bytes()
        assert submit(app, client, 'model', **values).status_code == 400
        assert (settings.paths.state / 'settings.json').read_bytes() == before
    # Arbitrary environment injection and local PDF execution are not form fields.
    assert submit(app, client, 'import', PAPER_MINERU_MODE='local', language='en').status_code == 400
    assert 'PAPER_MINERU_MODE' not in read_preferences(settings.paths.state)['values']


def test_failed_connection_never_replaces_working_settings(configured, monkeypatch):
    settings, app, client = configured
    def fail(settings):
        raise PreferenceError('connection failed')
    monkeypatch.setattr(connections, 'test_model', fail)
    assert submit(app, client, 'model', model='bad').status_code == 400
    assert not (settings.paths.state / 'settings.json').exists()
    assert app.extensions['runtime_preferences'].snapshot().translation().model == 'original'
    monkeypatch.setattr(connections, 'test_remote', fail)
    assert submit(app, client, 'remote', host='bad', authentication='password').status_code == 400
    assert app.extensions['runtime_preferences'].snapshot().values['PAPER_MINERU_REMOTE_HOST'] == '127.0.0.1'


@pytest.mark.parametrize('group,name,value', [
    ('model', 'timeout', '60'), ('model', 'max_tokens', '1000'), ('model', 'temperature', '1'),
    ('remote', 'python', '/other/python'), ('remote', 'root', '/other/parser'),
    ('remote', 'window', '16'), ('remote', 'timeout', '600'), ('import', 'PAPER_LIBRARY_PORT', '9999'),
])
def test_internal_parameters_are_neither_exposed_nor_mutable(configured, group, name, value):
    settings, app, client = configured
    page = BeautifulSoup(client.get('/manage/settings').data, 'html.parser')
    assert page.select_one(f'#{group} form [name="{name}"]') is None
    assert submit(app, client, group, **{name: value}).status_code == 400
    assert read_preferences(settings.paths.state)['values'] == {}
    assert not (settings.paths.state / 'settings.json').exists()


def test_probe_uses_unsaved_inputs_without_persisting(configured, monkeypatch):
    settings, app, client = configured
    calls = []
    monkeypatch.setattr(connections, 'test_model', lambda settings: calls.append(settings.translation().model) or {'message': 'ok'})
    response = client.post('/manage/settings/test', data={'management_token': app.extensions['management_guard'].token,
                           'revision': 'initial', 'group': 'model', 'model': 'candidate'})
    assert response.status_code == 200 and calls == ['candidate']
    assert not (settings.paths.state / 'settings.json').exists()


@pytest.mark.parametrize('value', [None, [], {'schema': 1}, {'schema': 1, 'revision': [], 'updated_at': None, 'values': {}}])
def test_corrupt_settings_do_not_break_reading_or_overwrite_file(configured, value):
    settings, _app, _client = configured
    path = settings.paths.state / 'settings.json'
    path.write_text(json.dumps(value))
    loaded = Settings.load(settings.paths, {})
    assert loaded.configuration_error
    app = create_app(loaded, start_jobs=False); client = app.test_client()
    assert client.get('/').status_code == 200
    assert client.get('/manage/settings').status_code == 200
    assert '设置文件不可用' in client.get('/manage/settings').get_data(as_text=True)
    assert submit_corrupt(app, client).status_code == 400
    assert json.loads(path.read_text()) == value
    with pytest.raises(PreferenceError): app.extensions['job_runner'].workflow_factory()


def submit_corrupt(app, client):
    return client.post('/manage/settings', data={'management_token': app.extensions['management_guard'].token,
                       'revision': 'initial', 'group': 'import', 'language': 'ch'})


def test_environment_fixed_fields_and_remote_bind_are_protected(configured):
    settings, _app, _client = configured
    loaded = Settings.load(settings.paths, {'PAPER_TRANSLATION_MODEL': 'fixed', 'PAPER_TRANSLATION_API_KEY': 'fixed-secret'})
    app = create_app(loaded, start_jobs=False); client = app.test_client()
    assert submit(app, client, 'model', model='changed').status_code == 400
    assert submit(app, client, 'model', api_key_clear='1').status_code == 400
    html = BeautifulSoup(client.get('/manage/settings').data, 'html.parser')
    assert html.select_one('#setting-model-model').has_attr('readonly')
    assert b'fixed-secret' not in client.get('/manage/settings').data
    remote = create_app(replace(settings, host='0.0.0.0', remote_management_enabled=True), start_jobs=False)
    assert remote.test_client().get('/manage/settings').status_code == 403
    assert remote.test_client().post('/manage/settings', data={'management_token': remote.extensions['management_guard'].token}).status_code == 403


def test_atomic_write_failure_and_concurrent_edit_conflict(configured, monkeypatch):
    settings, app, client = configured
    runtime = app.extensions['runtime_preferences']
    assert submit(app, client, 'import', language='ch').status_code == 200
    before = (settings.paths.state / 'settings.json').read_bytes()
    import paper_library.preferences as preferences
    original = preferences.os.replace
    def fail(*args): raise OSError('disk full')
    monkeypatch.setattr(preferences.os, 'replace', fail)
    assert submit(app, client, 'import', language='en').status_code == 503
    assert (settings.paths.state / 'settings.json').read_bytes() == before
    assert not list(settings.paths.state.glob('.settings-*'))
    monkeypatch.setattr(preferences.os, 'replace', original)
    revision = read_preferences(settings.paths.state)['revision']
    def save(value):
        try: runtime.save(revision, 'import', {'language': value}); return 'ok'
        except PreferenceConflict: return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(save, ['en', 'ch'])) == ['conflict', 'ok']


def test_preference_file_refuses_symlinks_and_arbitrary_keys(configured):
    settings, _app, _client = configured
    path = settings.paths.state / 'settings.json'
    target = settings.paths.root / 'outside'; target.write_text('{}')
    path.symlink_to(target)
    with pytest.raises(PreferenceError): read_preferences(settings.paths.state)
    assert target.read_text() == '{}'
    with pytest.raises(PreferenceError): validate_values({'PAPER_MINERU_MODE': 'local'})
