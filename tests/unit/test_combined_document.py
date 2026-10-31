import json

import httpx
import pytest

from paper_library.config import ProjectPaths, Settings
from paper_library.metadata.repair import title_clues, verify_bibliography
from paper_library.metadata.service import EnrichmentResult, GatewayResponse
from paper_library.reading.service import GuideService, _overview_source_sections, validate_points
from paper_library.structure.frontmatter import primary_article


TITLE = 'State media control influences large language models'
DOI = '10.1038/example-article'
SOURCE = f'''# Research briefing

# A short briefing about the research

This is a summary of: {TITLE}. https://doi.org/10.1038/cited-article

# References

Briefing references appear before the actual article.

# {TITLE}

https://doi.org/{DOI}

Jane Doe and John Smith

We investigate training data and report carefully bounded observations.

# Methods

We compare the same questions in two different languages.

# Reporting Summary

The reporting form has incomplete extracted fragments.

# Supplementary information

Supplementary examples are separate from the main text.
'''


def settings(tmp_path):
    return Settings.load(ProjectPaths.from_root(tmp_path), {
        'PAPER_TRANSLATION_PROFILE': 'test', 'LLM_TEST_MODEL': 'test',
        'LLM_TEST_BASE_URL': 'http://model/v1'})


def test_article_title_and_own_doi_are_located_after_briefing():
    article = primary_article(SOURCE)
    assert article.title == TITLE
    assert article.doi == DOI
    assert article.combined
    assert article.text.startswith('# ' + TITLE)
    assert SOURCE.splitlines()[article.end_line - 1] == '# Reporting Summary'
    clues = title_clues('filename', '2026-Nature-' + TITLE + '_Briefing&SI.pdf', SOURCE)
    assert ('markdown', TITLE) in clues
    assert ('filename', TITLE) in clues
    assert ('markdown', 'Research briefing') not in clues


def test_later_cited_title_cannot_identify_a_normal_article():
    source = f'# A Different Local Paper Title\n\nText without its own DOI.\n\n# References\n\n# {TITLE}\n\nhttps://doi.org/{DOI}'
    assert primary_article(source) is None
    # A DOI inside a quoted code example is also not front matter.
    assert primary_article(f'# Local Paper About Code Examples\n\n```\n# {TITLE}\nhttps://doi.org/{DOI}\n```') is None


def test_combined_verification_uses_own_record_and_still_rejects_wrong_title(monkeypatch):
    monkeypatch.setattr('paper_library.metadata.queries.generate_search_plan',
                        lambda *a, **kw: {'status': 'disabled'})
    class Gateway:
        def ready(self): return GatewayResponse(200, {})
        def search(self, query): return GatewayResponse(200, {'results': []})
        def extract(self, urls): return GatewayResponse(200, {'results': []})
    class Official:
        title = TITLE
        def lookup(self, doi):
            assert doi == DOI
            return EnrichmentResult('verified', 1, {'canonical_title': self.title, 'doi': doi},
                                    {'source_domain': 'api.crossref.org'})
    official = Official()
    result = verify_bibliography('filename', '2026', 'Nature', 'bundle.pdf', SOURCE,
                                Gateway(), official_lookup=official)
    assert result.status == 'verified'
    assert result.enrichment['matched_clues'] == ['markdown']
    official.title = 'A Completely Different Official Paper Title'
    result = verify_bibliography('filename', '2026', 'Nature', 'bundle.pdf', SOURCE,
                                Gateway(), official_lookup=official)
    assert result.status == 'conflict'


def test_short_fragments_are_skipped_without_calling_the_model(tmp_path):
    def forbidden(request):
        pytest.fail('A short fragment must not call the model')
    service = GuideService(settings(tmp_path), transport=httpx.MockTransport(forbidden))
    result = service._ask({'task': '章节导读'}, [{'id': 'source-1', 'text': 'I'}])
    assert result['points'] == result['details'] == []
    assert '至少 10 个字符' in result['skip_reason']
    assert service._ask({'task': '章节导读'}, [{'id': 'article', 'text': '# Article'}])['skip_reason']


def test_short_blocks_are_removed_from_model_evidence_context(tmp_path):
    def respond(request):
        payload = json.loads(json.loads(request.content)['messages'][1]['content'])
        assert [b['id'] for b in payload['blocks']] == ['source-2']
        assert {e['id'] for e in payload['source_excerpts']} == {'source-2'}
        result = {'role': '方法', 'points': [{'kind': '原文结论', 'text': '作者比较两种语言。',
                  'evidence': [{'excerpt_id': payload['source_excerpts'][0]['excerpt_id']}]}]}
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
                              'message': {'content': json.dumps(result)}}]})
    service = GuideService(settings(tmp_path), transport=httpx.MockTransport(respond))
    blocks = [{'id': 'article', 'text': '# Article'},
              {'id': 'source-2', 'text': 'We compare the same questions in two languages.'}]
    result = service._ask({'task': '章节导读', 'blocks': blocks}, blocks)
    assert validate_points(result['points'], blocks)


def test_bundle_overview_includes_main_article_and_excludes_briefing_and_forms(tmp_path):
    calls = []
    def respond(request):
        payload = json.loads(json.loads(request.content)['messages'][1]['content'])
        calls.append(payload)
        if 'blocks' in payload:
            evidence = {'excerpt_id': payload['source_excerpts'][0]['excerpt_id']}
        else:
            evidence = payload['sections'][0]['points'][0]['evidence'][0]
            titles = [s['section'] for s in payload['sections']]
            assert TITLE in titles and 'Methods' in titles
            assert 'A short briefing about the research' not in titles
            assert 'Reporting Summary' not in titles and 'Supplementary information' not in titles
        result = {'role': '研究说明', 'points': [{'kind': '原文结论', 'text': '证据有适用范围。',
                  'evidence': [evidence]}]}
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
                              'message': {'content': json.dumps(result)}}]})
    path = tmp_path / 'article.md'
    path.write_text(SOURCE + '\n# Sampling strategy\n\nI\n')
    service = GuideService(settings(tmp_path), transport=httpx.MockTransport(respond))
    guide = service.generate(path, progress=lambda _: None)
    sampling = next(s for s in guide['sections'] if s['title'] == 'Sampling strategy')
    assert sampling['parts'][0]['skip_reason']
    before = len(calls)
    import paper_library.reading.service as reading
    original = reading.OVERVIEW_VERSION
    try:
        reading.OVERVIEW_VERSION = 'next-overview-policy'
        service.generate(path, progress=lambda _: None)
    finally:
        reading.OVERVIEW_VERSION = original
    # Chapter caches survive a change to the overview selection policy.
    assert len(calls) == before + 1
    assert calls[-1]['task'] == '全文导读'


def test_mixed_section_points_need_evidence_entirely_within_main_article():
    def point(ids):
        return {'text': '主张', 'evidence': [{'id': i, 'quote': 'A long enough quote.'} for i in ids]}
    sections = [{'id': 'mixed', 'title': 'From the editor', 'parts': [
        {'points': [point(['briefing']), point(['main']), point(['briefing', 'main'])]}]}]
    selected = _overview_source_sections(sections, primary_ids={'main'})
    assert selected[0]['parts'][0]['points'] == [point(['main'])]
    assert len(sections[0]['parts'][0]['points']) == 3
