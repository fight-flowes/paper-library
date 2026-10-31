"""Private, versioned runtime preferences shared by HTTP, jobs and CLI commands."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
from uuid import uuid4
from urllib.parse import urlsplit


MODEL_FIELDS = {
    'base_url': 'PAPER_TRANSLATION_BASE_URL', 'model': 'PAPER_TRANSLATION_MODEL',
    'api_key': 'PAPER_TRANSLATION_API_KEY', 'request_style': 'PAPER_TRANSLATION_REQUEST_STYLE',
    'concurrency': 'PAPER_TRANSLATION_MAX_CONCURRENCY',
}
REMOTE_FIELDS = {name: 'PAPER_MINERU_REMOTE_' + suffix for name, suffix in (
    ('host', 'HOST'), ('port', 'PORT'), ('user', 'USER'),
    ('password', 'PASSWORD'), ('identity_file', 'IDENTITY_FILE'))}
IMPORT_FIELDS = {name: 'PAPER_IMPORT_' + name.upper() for name in ('library', 'language', 'enrich', 'auto_translate')}
FIELDS = {'model': MODEL_FIELDS, 'remote': REMOTE_FIELDS, 'import': IMPORT_FIELDS}
ALLOWED = frozenset(key for fields in FIELDS.values() for key in fields.values())
SECRETS = frozenset(('PAPER_TRANSLATION_API_KEY', 'PAPER_MINERU_REMOTE_PASSWORD'))


class PreferenceError(ValueError):
    pass


class PreferenceConflict(PreferenceError):
    pass


def _text(value, label, limit=2048, *, empty=False):
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise PreferenceError(label + '格式无效')
    value = value.strip()
    if not value and not empty:
        raise PreferenceError('请填写' + label)
    return value


def _number(value, label, minimum, maximum, *, integer=True):
    try:
        parsed = int(value) if integer else float(value)
    except (ValueError, TypeError, OverflowError):
        raise PreferenceError(label + '必须是有效数值') from None
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
        raise PreferenceError(f'{label}须在 {minimum} 到 {maximum} 之间')
    return str(parsed)


def validate_values(values):
    """Validate even hand-edited preference files; never accept arbitrary env keys."""
    if not isinstance(values, dict) or set(values) - ALLOWED:
        raise PreferenceError('设置包含不支持的字段')
    checked = {}
    for key, value in values.items():
        checked[key] = _text(value, '设置项', 8192 if key in SECRETS else 2048, empty=True)
        if key in SECRETS:
            checked[key] = value
    url = checked.get('PAPER_TRANSLATION_BASE_URL')
    if url:
        try:
            parsed = urlsplit(url)
            valid = parsed.scheme in ('http', 'https') and parsed.hostname and not (
                parsed.username or parsed.password or parsed.query or parsed.fragment)
            _ = parsed.port
        except ValueError:
            valid = False
        if not valid:
            raise PreferenceError('模型接口须为有效的 HTTP/HTTPS 地址，不含账号、查询参数或片段')
        checked['PAPER_TRANSLATION_BASE_URL'] = url.rstrip('/')
    style = checked.get('PAPER_TRANSLATION_REQUEST_STYLE')
    if style is not None and style not in ('openai', 'qwen', 'deepseek'):
        raise PreferenceError('请选择支持的模型接口格式')
    numeric = {
        'PAPER_TRANSLATION_MAX_CONCURRENCY': ('模型并发', 1, 32, True),
        'PAPER_MINERU_REMOTE_PORT': ('SSH 端口', 1, 65535, True),
    }
    for key, (label, low, high, integer) in numeric.items():
        if key in checked:
            checked[key] = _number(checked[key], label, low, high, integer=integer)
    for key in ('PAPER_MINERU_REMOTE_HOST', 'PAPER_MINERU_REMOTE_USER'):
        if key in checked and not re.fullmatch(r'[A-Za-z0-9_.:@\[\]-]+', checked[key]):
            raise PreferenceError('SSH 主机或用户名格式无效')
        if key in checked and checked[key].startswith('-'):
            raise PreferenceError('SSH 主机或用户名格式无效')
    for key in ('PAPER_MINERU_REMOTE_IDENTITY_FILE',):
        if checked.get(key):
            path = PurePosixPath(checked[key])
            if not path.is_absolute() or '..' in path.parts:
                raise PreferenceError('远程运行环境和 SSH 密钥路径须使用绝对路径')
    if 'PAPER_IMPORT_LANGUAGE' in checked and checked['PAPER_IMPORT_LANGUAGE'] not in ('en', 'ch'):
        raise PreferenceError('请选择英文或中文')
    for key in ('PAPER_IMPORT_ENRICH', 'PAPER_IMPORT_AUTO_TRANSLATE'):
        if key in checked and checked[key] not in ('0', '1'):
            raise PreferenceError('导入开关须为 0 或 1')
    if checked.get('PAPER_IMPORT_LIBRARY') and not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,100}', checked['PAPER_IMPORT_LIBRARY']):
        raise PreferenceError('论文库标识无效')
    return checked


def model_identity(settings):
    config = settings.translation(required=False)
    return dict(base_url=config.base_url, model=config.model, profile=config.profile,
                request_style=config.request_style)


def read_preferences(state):
    path = Path(state) / 'settings.json'
    if not path.exists() and not path.is_symlink():
        return {'schema': 1, 'revision': 'initial', 'updated_at': '', 'values': {}, 'model_history': []}
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_size > 1024 * 1024:
            raise ValueError()
        # Old private files may have inherited a broad umask; never leave secrets readable.
        if metadata.st_mode & 0o777 != 0o600:
            path.chmod(0o600)
        document = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(document, dict) or document.get('schema') != 1:
            raise ValueError()
        if not re.fullmatch(r'[a-f0-9]{32}', document.get('revision', '')):
            raise ValueError()
        stamp = datetime.fromisoformat(document['updated_at'])
        if stamp.tzinfo is None:
            raise ValueError()
        document['values'] = validate_values(document['values'])
        history = document.get('model_history', [])
        if not isinstance(history, list) or len(history) > 256:
            raise ValueError()
        for item in history:
            if not isinstance(item, dict) or set(item) != {'base_url', 'model', 'profile', 'request_style'}:
                raise ValueError()
            if not all(isinstance(value, str) and len(value) <= 2048 for value in item.values()):
                raise ValueError()
            validate_values({'PAPER_TRANSLATION_BASE_URL': item['base_url'],
                             'PAPER_TRANSLATION_REQUEST_STYLE': item['request_style']})
        document['model_history'] = history
        return document
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        raise PreferenceError('设置文件不可用；请检查 .paper-library-dev/settings.json。现有论文仍可阅读。') from None


@contextmanager
def preference_lock(state):
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True)
    path = state / 'settings.lock'
    flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, 'r+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def overlay_settings(settings, document):
    baseline = dict(settings.base_values or settings.values)
    baseline.update({key: value for key, value in document['values'].items()
                     if key not in settings.environment_keys})
    return replace(settings, values=baseline, revision=document['revision'], configuration_error='')


class RuntimePreferences:
    def __init__(self, settings):
        self.initial = settings

    def snapshot(self):
        return overlay_settings(self.initial, read_preferences(self.initial.paths.state))

    def candidate(self, document, group, form):
        if group not in FIELDS:
            raise PreferenceError('未知的设置分类')
        allowed_form = {'management_token', 'revision', 'group', *FIELDS[group]}
        allowed_form.update(name + '_clear' for name, key in FIELDS[group].items() if key in SECRETS)
        if group == 'remote':
            allowed_form.add('authentication')
        if set(form) - allowed_form:
            raise PreferenceError('该参数属于后端配置，不能从设置页修改')
        current = overlay_settings(self.initial, document)
        values = dict(document['values'])
        for name, key in FIELDS[group].items():
            if key in self.initial.environment_keys:
                # Fields controlled by the process environment are rendered readonly.
                if key in SECRETS and (form.get(name) or form.get(name + '_clear') == '1'):
                    raise PreferenceError('该密钥由环境变量固定，请先在启动环境中修改')
                if name in form and key not in SECRETS:
                    shown = public_settings(current)[group].get(name, '')
                    if str(form[name]) != str(shown):
                        raise PreferenceError('该设置由环境变量固定，请先在启动环境中修改')
                continue
            if key in SECRETS:
                if form.get(name + '_clear') == '1':
                    values[key] = ''
                elif form.get(name):
                    values[key] = form[name]
            elif name in form:
                values[key] = form[name]
        values = validate_values(values)
        proposed = overlay_settings(self.initial, {**document, 'values': values})
        if group == 'model':
            model = proposed.translation(required=False)
            if not model.configured:
                raise PreferenceError('请填写模型接口地址和模型名称')
        if group == 'remote':
            from paper_library.ingest.runtime import parser_command
            parser_command(proposed.values)
            auth = form.get('authentication', 'password')
            if auth not in ('password', 'key'):
                raise PreferenceError('请选择 SSH 认证方式')
            if auth == 'key':
                if 'PAPER_MINERU_REMOTE_PASSWORD' in self.initial.environment_keys and proposed.values.get('PAPER_MINERU_REMOTE_PASSWORD'):
                    raise PreferenceError('SSH 密码由环境变量固定，无法切换认证方式')
                values['PAPER_MINERU_REMOTE_PASSWORD'] = ''
            else:
                if not proposed.values.get('PAPER_MINERU_REMOTE_PASSWORD'):
                    raise PreferenceError('请填写 SSH 密码，或选择 SSH 密钥认证')
            proposed = overlay_settings(self.initial, {**document, 'values': values})
        return values, proposed

    def save(self, revision, group, form, *, validate=None):
        with preference_lock(self.initial.paths.state):
            document = read_preferences(self.initial.paths.state)
            if revision != document['revision']:
                raise PreferenceConflict('设置已在其他窗口更新，请刷新后重试')
            values, proposed = self.candidate(document, group, form)
            if validate:
                validate(proposed)
            history = list(document['model_history'])
            for config in (overlay_settings(self.initial, document), proposed):
                identity = model_identity(config)
                if identity['base_url'] and identity['model'] and identity not in history:
                    history.append(identity)
            if len(history) > 256:
                raise PreferenceError('历史模型配置数量已达上限，请先整理配置记录')
            updated = dict(schema=1, revision=uuid4().hex, updated_at=datetime.now(timezone.utc).isoformat(),
                           values=values, model_history=history)
            root = self.initial.paths.state
            fd, name = tempfile.mkstemp(prefix='.settings-', dir=root)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    json.dump(updated, stream, ensure_ascii=False, indent=2)
                    stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
                os.replace(name, root / 'settings.json')
                directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
                try: os.fsync(directory)
                finally: os.close(directory)
            finally:
                Path(name).unlink(missing_ok=True)
            return updated


def public_settings(settings):
    model = settings.translation(required=False)
    values = settings.values
    return {
        'model': dict(base_url=model.base_url, model=model.model, request_style=model.request_style,
                      concurrency=model.concurrency, api_key_configured=bool(model.api_key)),
        'remote': dict(host=values.get('PAPER_MINERU_REMOTE_HOST', ''), port=values.get('PAPER_MINERU_REMOTE_PORT', '22'),
                       user=values.get('PAPER_MINERU_REMOTE_USER', ''), identity_file=values.get('PAPER_MINERU_REMOTE_IDENTITY_FILE', ''),
                       password_configured=bool(values.get('PAPER_MINERU_REMOTE_PASSWORD')),
                       authentication='password' if values.get('PAPER_MINERU_REMOTE_PASSWORD') else 'key'),
        'import': dict(library=values.get('PAPER_IMPORT_LIBRARY', ''), language=values.get('PAPER_IMPORT_LANGUAGE', 'en'),
                       enrich=values.get('PAPER_IMPORT_ENRICH', '1'), auto_translate=values.get('PAPER_IMPORT_AUTO_TRANSLATE', '1')),
    }


class ReadingTranslations:
    """Read saved translations from current and previous models; never generate."""
    def __init__(self, runtime):
        self.runtime = runtime

    @property
    def config(self):
        return self._service().config

    @property
    def prompt_fingerprint(self):
        try: revision = read_preferences(self.runtime.initial.paths.state)['revision']
        except PreferenceError: revision = 'unavailable'
        return self._service().prompt_fingerprint + ':' + revision

    def _service(self, settings=None, identity=None):
        from paper_library.translation.service import TranslationConfig, TranslationService
        if settings is None:
            try: settings = self.runtime.snapshot()
            except PreferenceError: settings = self.runtime.initial
        config = TranslationConfig.from_settings(settings.translation(required=False))
        if identity:
            config = replace(config, **identity, api_key='')
        return TranslationService(config, settings.paths.cache, max_concurrency=config.concurrency)

    def cached_many_records(self, paper, version, segments):
        segments = list(segments)
        primary = self._service()
        result = primary.cached_many_records(paper, version, segments)
        try: history = read_preferences(self.runtime.initial.paths.state)['model_history']
        except PreferenceError: history = []
        history = [model_identity(self.runtime.initial), *reversed(history)]
        seen = {(primary.config.base_url, primary.config.model, primary.config.request_style)}
        for identity in history:
            key = (identity['base_url'], identity['model'], identity['request_style'])
            if key in seen or len(result) == len(segments):
                continue
            seen.add(key)
            missing = [segment for segment in segments if segment.id not in result]
            result.update(self._service(identity=identity).cached_many_records(paper, version, missing))
        return result

    def cached_many(self, paper, version, segments):
        return {key: record.text for key, record in self.cached_many_records(paper, version, segments).items()}

    def revision(self, paper):
        return self._service().revision(paper)
