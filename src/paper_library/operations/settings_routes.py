"""The single-user settings surface; secrets are write-only."""
from threading import BoundedSemaphore

from flask import Blueprint, abort, current_app, jsonify, redirect, render_template, request, url_for

from paper_library.catalog import LibraryError
from paper_library.preferences import (FIELDS, PreferenceConflict, PreferenceError, overlay_settings,
                                       public_settings, read_preferences)
from . import connections


def settings_blueprint(runtime, guard, libraries):
    bp = Blueprint('settings', __name__)
    probes = BoundedSemaphore(1)

    @bp.after_request
    def private_response(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'same-origin'
        return response

    def authorize():
        # Server paths and credentials are local single-user administration.
        from paper_library.jobs.security import _is_loopback
        if not _is_loopback(runtime.initial.host) or not guard.mutations_enabled or not guard.validate(request.form.get('management_token')):
            abort(403)
        origin = request.headers.get('Origin')
        if origin and origin != request.host_url.rstrip('/'):
            abort(403)
        if request.headers.get('Sec-Fetch-Site') == 'cross-site':
            abort(403)

    def validate_import(proposed):
        library_id = proposed.values.get('PAPER_IMPORT_LIBRARY', '')
        if library_id:
            try: libraries.load(library_id)
            except (LibraryError, ValueError, OSError):
                raise PreferenceError('默认论文库不可用，请重新选择') from None

    @bp.get('/manage/settings')
    def page():
        from paper_library.jobs.security import _is_loopback
        if not _is_loopback(runtime.initial.host):
            abort(403)
        error = None
        try:
            document = read_preferences(runtime.initial.paths.state)
            snapshot = overlay_settings(runtime.initial, document)
        except PreferenceError as exc:
            error = str(exc)
            snapshot = runtime.initial
            document = {'revision': '', 'updated_at': ''}
        available, _issues = libraries.scan()
        runner = current_app.extensions.get('job_runner')
        active = runner.status() if hasattr(runner, 'status') else {}
        locked = {group: [name for name, key in fields.items() if key in snapshot.environment_keys]
                  for group, fields in FIELDS.items()}
        return render_template('settings.html', preferences=public_settings(snapshot),
                               revision=document['revision'], updated_at=document['updated_at'],
                               libraries=available, locked=locked, active=active,
                               error=error, management_token=guard.token,
                               mutations_enabled=guard.mutations_enabled and not error)

    @bp.post('/manage/settings')
    def save():
        authorize()
        group = request.form.get('group', '')
        try:
            validator = {'import': validate_import, 'model': connections.test_model,
                         'remote': connections.test_remote}.get(group)
            result = runtime.save(request.form.get('revision'), group, request.form,
                                  validate=validator)
        except PreferenceConflict as exc:
            return jsonify(error=str(exc)), 409
        except PreferenceError as exc:
            return jsonify(error=str(exc)), 400
        except OSError:
            return jsonify(error='设置无法保存，请检查目录权限和磁盘空间'), 503
        if request.accept_mimetypes.best == 'application/json':
            return jsonify(revision=result['revision'], updated_at=result['updated_at'],
                           message='已保存并生效；运行中的任务继续使用原配置',
                           preferences=public_settings(runtime.snapshot()))
        return redirect(url_for('settings.page', saved=group) + '#' + group, code=303)

    @bp.post('/manage/settings/test')
    def test_connection():
        authorize()
        group = request.form.get('group', '')
        if group not in ('model', 'remote'):
            return jsonify(error='请选择模型或解析连接测试'), 400
        if not probes.acquire(blocking=False):
            return jsonify(error='已有连接测试正在进行，请稍后再试'), 429
        try:
            document = read_preferences(runtime.initial.paths.state)
            if request.form.get('revision') != document['revision']:
                raise PreferenceConflict('设置已更新，请刷新后测试')
            _values, proposed = runtime.candidate(document, group, request.form)
            result = connections.test_model(proposed) if group == 'model' else connections.test_remote(proposed)
            return jsonify(**result)
        except PreferenceConflict as exc:
            return jsonify(error=str(exc)), 409
        except PreferenceError as exc:
            return jsonify(error=str(exc)), 400
        finally:
            probes.release()

    return bp
