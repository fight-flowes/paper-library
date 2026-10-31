import csv
from bs4 import BeautifulSoup

from test_reader import make_settings
from paper_library.web.app import create_app
from paper_library.web.markdown import render_translatable_markdown, render_markdown
from paper_library.web.presentation import reading_navigation


def test_catalog_filters_combine_and_sort_without_changing_catalog(tmp_path):
    settings = make_settings(tmp_path)
    catalog = settings.paths.libraries/'example'/'catalog.tsv'
    with catalog.open('a') as stream:
        writer = csv.writer(stream, delimiter='\t')
        writer.writerow(['ex-p0002','2024','Nature','','topic','Earlier study','hash','2026-02-01T00:00:00Z','',''])
        writer.writerow(['ex-p0003','2025','ACL','','other','Another study','hash','2026-03-01T00:00:00Z','',''])
    original = catalog.read_bytes()
    client = create_app(settings, start_jobs=False).test_client()
    def titles(query):
        response = client.get('/library/example/', query_string=query)
        assert response.status_code == 200
        return [h.text for h in BeautifulSoup(response.data, 'html.parser').select('.paper-row h3')]
    assert titles({'subject':'topic','venue':'ACL','year':'2026','q':'EXAMPLE'}) == ['Example Paper']
    assert titles({'subject':'topic','sort':'imported'}) == ['Earlier study','Example Paper']
    assert titles({'subject':'topic','sort':'year'}) == ['Example Paper','Earlier study']
    assert titles({'q':'<script>no-match</script>'}) == []
    page = client.get('/library/example/?q=no-match').get_data(as_text=True)
    assert '没有匹配的论文' in page and '清除筛选' in page
    assert catalog.read_bytes() == original
    imported = BeautifulSoup(client.get('/manage/import?library=example').data, 'html.parser')
    assert imported.select_one('#import-library option[selected]')['value'] == 'example'


def test_combined_navigation_uses_physical_article_and_keeps_all_headings():
    source = ('# Research briefing\n\nBrief text.\n\n# Article\n\n'
              '# Main article title\n\nhttps://doi.org/10.1038/s41586-026-12345-6\n\n'
              'Article body.\n\n## Methods\n\nMethod text.\n\n# Reporting summary\n\nForm data.\n')
    rendered = render_translatable_markdown(source, '/asset')
    navigation = reading_navigation(source, rendered.headings)
    assert navigation['combined'] and navigation['main'].text == 'Main article title'
    assert [g['key'] for g in navigation['groups']] == ['briefing','article','supplement']
    assert [h.id for g in navigation['groups'] for h in g['headings']] == [h.id for h in rendered.headings]
    from paper_library.structure.document import build_blocks
    annotations=[dict(id=b['id'],role='title',toc=False) if b['id']=='source-7' else dict(id=b['id'],role='unknown') for b in build_blocks(source)]
    structured=render_markdown(source,'/asset',annotations=annotations)
    assert not any(h.source_line==7 for h in structured.headings)
    navigation=reading_navigation(source,structured.headings,structured.physical_headings)
    assert navigation['combined'] and navigation['main'].id=='main-article-title'
    ambiguous = source.replace('# Reporting summary', '# Another article\n\nhttps://doi.org/10.1038/s41586-026-54321-0\n\n# Reporting summary')
    navigation = reading_navigation(ambiguous, render_translatable_markdown(ambiguous, '/asset').headings)
    assert navigation['main'] is None and not navigation['combined']


def test_display_image_attributes_preserve_translation_fragments():
    source = '# Paper\n\nAn English caption with ![figure](images/test.png) and a formula $x+1$.\n'
    rendered = render_translatable_markdown(source, '/asset')
    assert 'loading="lazy"' in rendered.html and 'decoding="async"' in rendered.html
    protected = ' '.join(value for segment in rendered.segments for _, value in segment.protected)
    assert 'loading=' not in protected and 'decoding=' not in protected
