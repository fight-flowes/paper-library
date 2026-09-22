import csv
import hashlib
import json

from paper_library.catalog import PaperPaths
from paper_library.web.app import create_app
from test_management_routes import make_settings


def test_reader_escapes_guidance_and_hides_stale_content(tmp_path):
    settings = make_settings(tmp_path)
    root = settings.paths.libraries / 'whitespace'
    paths = PaperPaths.for_id(root, 'ws-p0001')
    paths.root.mkdir(parents=True)
    source = '# A peculiar section\n\nOriginal supporting sentence.\n'
    paths.markdown.write_text(source)
    paths.metadata.write_text('{}')
    with (root / 'catalog.tsv').open('a') as stream:
        csv.writer(stream, delimiter='\t').writerow(['ws-p0001', '2025', '', '', 'topic', 'Paper', 'hash', '', '', ''])
    point = {'label': 'test', 'kind': '原文结论', 'text': '<script>bad()</script>',
             'evidence': [{'id': 'source-3', 'quote': 'Original supporting sentence.'}]}
    data = {'schema_version': 1, 'document_version': hashlib.sha256(source.encode()).hexdigest(),
            'overview': [point], 'sections': [{'id': 'a-peculiar-section', 'title': 'A peculiar section',
                'parts': [{'role': '解释方法', 'points': [point], 'details': [point]}]}],
            'model': 'test', 'generated_at': '2026-09-20', 'limitations': 'test'}
    guide = paths.root / 'reading-guide.json'
    guide.write_text(json.dumps(data))
    client = create_app(settings, start_jobs=False).test_client()
    page = client.get('/library/whitespace/paper/ws-p0001').get_data(as_text=True)
    assert '&lt;script&gt;bad()&lt;/script&gt;' in page
    assert '<script>bad()</script>' not in page
    assert 'href="#source-3"' in page and 'id="source-3"' in page
    assert '本节概览' in page and '展开详细解读' in page
    assert '<details class="guide-depth">' in page
    paths.markdown.write_text(source + '\nChanged.\n')
    page = client.get('/library/whitespace/paper/ws-p0001').get_data(as_text=True)
    assert '请重新生成导读' in page
    assert 'bad()' not in page
