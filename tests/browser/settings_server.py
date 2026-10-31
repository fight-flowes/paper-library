"""Isolated settings browser fixture, with a local fake text-model API."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import signal
import tempfile
from threading import Thread

from paper_library.catalog import CATALOG_COLUMNS
from paper_library.config import ProjectPaths, Settings
from paper_library.operations import connections
from paper_library.preferences import PreferenceError
from paper_library.web.app import create_app


class Model(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
        if self.headers.get('Authorization') not in ('Bearer fixture-secret', 'Bearer replacement-secret'):
            code, data = 401, {'error': 'sensitive response must never be shown'}
        elif body.get('model') == 'unavailable':
            code, data = 503, {'error': 'sensitive internal response'}
        else:
            code, data = 200, {'choices': [{'finish_reason': 'stop', 'message': {'content': 'OK'}}]}
        raw = json.dumps(data).encode()
        self.send_response(code); self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)


def main():
    def shutdown(*args): raise SystemExit()
    signal.signal(signal.SIGTERM, shutdown)
    root = Path(tempfile.mkdtemp(prefix='paper-settings-browser-'))
    model = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    Thread(target=model.serve_forever, daemon=True).start()
    try:
        paths = ProjectPaths.from_root(root)
        library = paths.libraries / 'example'; library.mkdir(parents=True)
        (library / 'library.toml').write_text('schema_version=1\nid="example"\nname="示例论文库"\npaper_id_prefix="ex"\n')
        (library / 'catalog.tsv').write_text('\t'.join(CATALOG_COLUMNS) + '\n')
        paths.dotenv.write_text(
            f'PAPER_TRANSLATION_PROFILE=fixture-model\nLLM_FIXTURE_MODEL=fixture-model\n'
            f'LLM_FIXTURE_BASE_URL=http://127.0.0.1:{model.server_port}/v1\nLLM_FIXTURE_API_KEY=fixture-secret\n'
            'PAPER_MINERU_REMOTE_HOST=127.0.0.1\nPAPER_MINERU_REMOTE_PORT=12222\n'
            'PAPER_MINERU_REMOTE_USER=fixture\nPAPER_MINERU_REMOTE_PASSWORD=fixture-ssh-secret\n'
            'PAPER_MINERU_REMOTE_PYTHON=/remote/env/bin/python\nPAPER_MINERU_REMOTE_ROOT=/remote/parser\n')
        settings = Settings.load(paths, {'PAPER_LIBRARY_PORT': os.getenv('PAPER_SETTINGS_TEST_PORT', '18022')})
        def remote_probe(proposed):
            if proposed.values['PAPER_MINERU_REMOTE_HOST'] == 'broken':
                raise PreferenceError('cicore01 连接失败；不会回退本机解析')
            return {'message': 'cicore01 解析环境可用', 'gpu': 'Fixture GPU', 'seconds': .01}
        connections.test_remote = remote_probe
        app = create_app(settings, start_jobs=False)
        app.run(host='127.0.0.1', port=settings.port, debug=False, use_reloader=False)
    finally:
        model.shutdown(); shutil.rmtree(root)


if __name__ == '__main__': main()
