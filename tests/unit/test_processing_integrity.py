import hashlib
import json
import sqlite3

import httpx
import pytest

from paper_library.config import ProjectPaths, Settings
from paper_library.metadata.identifiers import CrossrefClient
from paper_library.metadata.repair import verify_bibliography
from paper_library.metadata.service import EnrichmentResult, GatewayResponse, _extract_bibliography, _arxiv_id
from paper_library.reading.scope import overview_ids
from paper_library.reading.service import GuideService, load_guide
from paper_library.reading.structure import build_document
from paper_library.structure.document import build_blocks
from paper_library.structure.frontmatter import primary_article
from paper_library.translation.batch import TranslationTask, run_batch
from paper_library.translation.service import TranslationConfig, TranslationService
from paper_library.web.markdown import render_markdown


TITLE = 'A Clear Paper About Language Models'
URL = 'https://www.nature.com/articles/example'


class Gateway:
    def ready(self): return GatewayResponse(200, {})
    def search(self, query):
        return GatewayResponse(200, {'results': [{'title': TITLE, 'url': URL, 'content': TITLE}]})
    def extract(self, urls):
        return GatewayResponse(200, {'results': [{'url': url, 'raw_content': '# ' + TITLE} for url in urls]})


@pytest.fixture(autouse=True)
def no_search_model(monkeypatch):
    monkeypatch.setattr('paper_library.metadata.queries.generate_search_plan', lambda *a, **k: {'status': 'disabled'})


def test_wrong_filename_cannot_overrule_an_independent_body_title():
    result = verify_bibliography(TITLE, '', '', TITLE + '.pdf',
                                '# A Completely Different Scientific Paper', Gateway())
    assert result.status in {'ambiguous', 'conflict'}
    assert result.bibliography == {}


def test_a_heading_inside_a_code_example_is_not_a_local_title():
    source = f'```text\n# {TITLE}\n```\n\n# A Completely Different Scientific Paper\n\nBody.'
    result = verify_bibliography(TITLE, '', '', TITLE + '.pdf', source, Gateway())
    assert result.status != 'verified'


def test_doi_in_an_example_or_inline_citation_is_not_the_article_identifier():
    class Forbidden:
        def lookup(self, doi):
            pytest.fail('A code example or citation is not the article DOI')
    source = f'# {TITLE}\n\nAn example follows.\n\n```\nDOI: 10.1000/example\n```\n\nWe cite https://doi.org/10.1000/cited.'
    result = verify_bibliography(TITLE, '', '', 'paper.pdf', source, Gateway(), official_lookup=Forbidden())
    assert result.status == 'verified'


def test_metadata_refresh_never_rewrites_a_code_example_heading(tmp_path, monkeypatch):
    from test_metadata_repair import fixture_paper, gateway, TITLE as canonical
    from paper_library.ingest.service import refresh_metadata
    repository, _, paths = fixture_paper(tmp_path, monkeypatch)
    partial = 'Vulnerability of LLMs to'
    example = '```text\n# ' + partial + '\n```'
    paths.markdown.write_text(example + '\n\n# ' + partial + '\n\nAuthors')
    refresh_metadata(repository, 'whitespace', 'ws-p0001', gateway=gateway())
    updated = paths.markdown.read_text()
    assert updated.startswith(example)
    assert '\n\n# ' + canonical + '\n' in updated


@pytest.mark.parametrize('url,content', [
    (URL, 'The page mentions ' + TITLE + ' but contains no article heading.'),
    ('https://arxiv.org/abs/2601.12345', '# Abstract\nThe page cites ' + TITLE),
    ('https://arxiv.org/abs/2601.12345', '# Title:\n\nAuthors: Jane Doe'),
])
def test_page_without_observed_title_cannot_verify_itself_from_the_query(url, content):
    assert _extract_bibliography(TITLE, url, {'results': [{'url': url, 'raw_content': content}]}) == {}


def test_publisher_navigation_does_not_replace_the_article_title():
    result = _extract_bibliography(TITLE, URL, {'results': [{'url': URL,
        'raw_content': '# Nature\n## Menu\n# ' + TITLE + '\n## Abstract\nText.'}]})
    assert result['canonical_title'] == TITLE


def test_doi_parentheses_and_plus_signs_survive_frontmatter_extraction():
    doi = '10.1000/example(2026)+version-2'
    class Official:
        def lookup(self, value):
            assert value == doi
            return EnrichmentResult('verified', 1, {'canonical_title': TITLE, 'doi': doi},
                                    {'source_domain': 'api.crossref.org'})
    result = verify_bibliography(TITLE, '', '', 'paper.pdf',
                                f'# {TITLE}\n\nhttps://doi.org/{doi}', Gateway(), official_lookup=Official())
    assert result.status == 'verified'


def test_a_doi_url_and_a_bare_doi_do_not_create_a_false_identifier_conflict():
    class Official:
        def lookup(self, value):
            assert value == '10.1000/example'
            return EnrichmentResult('verified', 1, {'canonical_title': TITLE, 'doi': value},
                                    {'source_domain': 'api.crossref.org'})
    result = verify_bibliography(TITLE, '', '', 'paper.pdf', '# ' + TITLE, Gateway(),
        known={'doi': 'HTTPS://DX.DOI.ORG/10.1000/example'}, official_lookup=Official())
    assert result.status == 'verified'


@pytest.mark.parametrize('crossref_conflict', [False, True])
def test_identifier_conflict_cannot_be_erased_by_a_later_record_without_identifier(crossref_conflict):
    class Official:
        def lookup(self, doi):
            if crossref_conflict:
                return EnrichmentResult('conflict', None, {}, {'message': 'Wrong DOI returned'})
            return EnrichmentResult('verified', 1, {'canonical_title': TITLE, 'doi': '10.1000/wrong'},
                                    {'source_domain': 'api.crossref.org'})
    result = verify_bibliography(TITLE, '', '', 'paper.pdf', '# ' + TITLE, Gateway(),
                                known={'doi': '10.1000/expected'}, official_lookup=Official())
    assert result.status == 'conflict'
    assert result.bibliography == {}


def test_preprint_doi_does_not_conflict_with_its_officially_linked_published_doi():
    url = 'https://arxiv.org/abs/2601.12345'
    class Arxiv(Gateway):
        def extract(self, urls):
            return GatewayResponse(200, {'results': [{'url': url, 'raw_content': f'# Title:{TITLE}\n'
                '| Journal reference: | Proceedings of Example Conference 2026 |\n'
                '| Related DOI: | <https://doi.org/10.1000/published> |'}]})
    result = verify_bibliography(TITLE, '', '', '2601.12345.pdf',
                                f'# {TITLE}\n\nhttps://doi.org/10.48550/arXiv.2601.12345', Arxiv())
    assert result.status == 'verified'
    assert result.bibliography['doi'] == '10.1000/published'


def test_invalid_crossref_date_uses_the_next_valid_date_and_keeps_corporate_authors():
    client = CrossrefClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={'message': {
        'DOI': '10.1000/test', 'title': [TITLE],
        'author': [{'name': 'Research Consortium'}, {'given': ['invalid'], 'family': None}],
        'published': {'date-parts': [[2026, 99, 40]]},
        'published-online': {'date-parts': [[2026, 10, 26]]},
    }})))
    record = client.lookup('10.1000/test').bibliography
    assert record['publication_date'] == '2026-10-26'
    assert record['authors'] == ['Research Consortium']


def test_arxiv_identifier_requires_a_complete_paper_path():
    assert _arxiv_id('https://arxiv.org/abs/2601.12345/unrelated') is None
    assert _arxiv_id('https://arxiv.org/html/2601.12345v2/') == '2601.12345'


def test_short_article_titles_are_valid_but_a_bundle_with_two_distinct_records_is_ambiguous():
    assert primary_article('# BERT\n\nhttps://doi.org/10.1000/bert').title == 'BERT'
    source = ('# Research briefing\n\n# A Briefing About This Research\n\nhttps://doi.org/10.1000/briefing\n\n'
              f'# {TITLE}\n\nhttps://doi.org/10.1000/article\n\nThe actual article body.')
    assert primary_article(source) is None
    with pytest.raises(ValueError, match='无法唯一确定'):
        overview_ids(source, build_document(source))


@pytest.mark.parametrize('boundary', ['# References', '# Appendix A', '# Supplementary materials:'])
def test_overview_respects_source_boundaries_even_when_the_toc_hides_them(boundary):
    source = '# Study\n\nMain evidence is described here.\n\n' + boundary + '\n\nMaterial outside the main text.'
    roles = [{'id': b['id'], 'role': 'body'} for b in build_blocks(source)]
    document = build_document(source, roles)
    ids = overview_ids(source, document, roles)
    assert 'source-3' in ids
    assert 'source-7' not in ids


def test_nature_methods_after_references_remain_in_main_overview():
    source = (f'# {TITLE}\n\nhttps://doi.org/10.1038/example\n\nMain observation with evidence.\n\n'
              '# References\n\nA cited paper outside the main claims.\n\n'
              '# Methods\n\nThe method describes how observations were collected.\n\n'
              '# Supplementary information\n\nExtra examples outside the main article.')
    document = build_document(source)
    ids = overview_ids(source, document)
    assert 'source-5' in ids and 'source-13' in ids
    assert 'source-9' not in ids and 'source-17' not in ids


def guide_service(tmp_path):
    settings = Settings.load(ProjectPaths.from_root(tmp_path), {
        'PAPER_TRANSLATION_PROFILE': 'test', 'LLM_TEST_MODEL': 'test', 'LLM_TEST_BASE_URL': 'http://model/v1'})
    def respond(request):
        payload = json.loads(json.loads(request.content)['messages'][1]['content'])
        evidence = ({'excerpt_id': payload['source_excerpts'][0]['excerpt_id']} if 'blocks' in payload
                    else payload['sections'][0]['points'][0]['evidence'][0])
        value = {'role': '研究证据', 'points': [{'kind': '原文结论', 'text': '证据有适用条件。', 'evidence': [evidence]}]}
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(value)}}]})
    return GuideService(settings, transport=httpx.MockTransport(respond))


@pytest.mark.parametrize('corruption', ['quote', 'sections', 'empty_part', 'bad_part'])
def test_published_guide_content_is_checked_even_when_document_version_matches(tmp_path, corruption):
    path = tmp_path / 'article.md'
    path.write_text('# Study\n\nThe observation has a specific experimental condition.')
    service = guide_service(tmp_path)
    guide = service.generate(path, progress=lambda _: None)
    version = guide['document_version']
    if corruption == 'quote': guide['overview'][0]['evidence'][0]['quote'] = 'An invented quote outside the source.'
    elif corruption == 'sections': guide['sections'] = []
    elif corruption == 'empty_part': guide['sections'][0]['parts'] = []
    else: guide['sections'][0]['parts'] = [None]
    (tmp_path / 'reading-guide.json').write_text(json.dumps(guide))
    assert load_guide(tmp_path, version)['stale']
    # The complete, validated chapter cache can repair the published file.
    service.generate(path, progress=lambda _: None)
    assert not load_guide(tmp_path, version)['stale']


def test_empty_chapter_cache_requires_a_valid_skip_reason(tmp_path):
    service = guide_service(tmp_path)
    blocks = [{'id': 'source-1', 'text': 'Evidence from the original article.'}]
    payload = {'task': '章节导读', 'blocks': blocks}
    key = hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()
    (tmp_path / (key + '.json')).write_text(json.dumps({'role': '', 'points': [], 'details': []}))
    result = service._cached_ask(payload, blocks, tmp_path, False)
    assert result['points']


def test_overview_only_upgrade_preserves_chapters_and_backs_up_the_previous_guide(tmp_path, monkeypatch):
    path = tmp_path / 'article.md'
    path.write_text('# Study\n\nThe observation has a specific experimental condition.')
    service = guide_service(tmp_path)
    original = service.generate(path, progress=lambda _: None)
    import paper_library.reading.service as reading
    monkeypatch.setattr(reading, 'OVERVIEW_VERSION', 'next-overview-rule')
    ask = service._ask
    def only_overview(payload, blocks):
        assert payload['task'].startswith('全文导读')
        return ask(payload, blocks)
    service._ask = only_overview
    updated = service.generate(path, overview_only=True, progress=lambda _: None)
    assert updated['sections'] == original['sections']
    assert updated['generated_at'] == original['generated_at']
    assert updated['overview_version'] == 'next-overview-rule'
    assert next(tmp_path.glob('reading-guide-repairs/*/reading-guide.json')).is_file()


def test_valid_appendix_quote_cannot_be_used_in_a_main_overview_with_current_policy(tmp_path):
    path = tmp_path / 'article.md'
    path.write_text('# Study\n\nThe main observation has a specific condition.\n\n# Appendix A\n\nAn example belongs only to the appendix.')
    guide = guide_service(tmp_path).generate(path, progress=lambda _: None)
    guide['overview'] = guide['sections'][1]['parts'][0]['points']
    (tmp_path / 'reading-guide.json').write_text(json.dumps(guide))
    assert load_guide(tmp_path, guide['document_version'])['stale']


def test_short_fragment_notice_survives_integration_with_a_valid_chunk(tmp_path):
    service = guide_service(tmp_path)
    point = {'kind': '原文结论', 'text': '正常内容', 'evidence': [{'id': 'source-1', 'quote': 'An original source quote.'}]}
    parts = [{'role': '', 'points': [point], 'details': [], 'skip_reason': ''},
             {'role': '', 'points': [], 'details': [], 'skip_reason': '原文信息不足'}]
    merged = service._integrate_section({'title': 'Methods'}, parts, tmp_path, False)
    assert merged[0]['points'] and merged[1]['skip_reason'] == '原文信息不足'


def translation_service(tmp_path, handler):
    return TranslationService(TranslationConfig('http://model/v1', 'test', ''), tmp_path / 'translations.sqlite3',
                              transport=httpx.MockTransport(handler))


def completion(text):
    return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': text}}]})


def test_heading_repair_does_not_request_the_number_kept_outside_the_model(tmp_path):
    calls = []
    def handler(request):
        data = json.loads(json.loads(request.content)['messages'][-1]['content'])
        calls.append(data)
        if len(calls) == 1: return completion('结果')
        assert data['required_markers'] == ['⟦PH_1⟧']
        assert data['missing_markers'] == ['⟦PH_1⟧']
        return completion('关于 ⟦PH_1⟧ 的结果')
    segment = render_markdown('# 2 Results for $x$', '/asset', translation_slots=True).segments[0]
    service = translation_service(tmp_path, handler)
    translated = service.translate('paper', 'v', segment)
    assert not segment.integrity_warnings(translated)
    assert segment.render_translation(translated).startswith('2 ')
    assert len(calls) == 2


def test_cache_notices_are_recomputed_from_text_instead_of_trusting_stored_flags(tmp_path):
    service = translation_service(tmp_path, lambda _: completion('译文省略了数字'))
    segment = render_markdown('We evaluate 12 examples.', '/asset', translation_slots=True).segments[0]
    service.translate('paper', 'v', segment)
    with sqlite3.connect(service.cache_path) as connection:
        connection.execute("UPDATE translations SET warnings='[]'")
    assert service.cached_record('paper', 'v', segment).warnings


def test_retry_integrity_repairs_content_loss_without_retranslating_advisory_notices(tmp_path):
    severe, soft = [render_markdown(s, '/asset', translation_slots=True).segments[0]
                    for s in ['We evaluate 12 examples.', 'Another English paragraph.']]
    service = translation_service(tmp_path, lambda _: completion('可阅读译文 7'))
    service.translate('library/paper', 'v', severe)
    service.translate('library/paper', 'v', soft)
    calls = []
    def repaired(request):
        calls.append(request)
        return completion('我们评估了 ⟦PH_0⟧ 个样本。')
    service.transport = httpx.MockTransport(repaired)
    tasks = [TranslationTask('library', 'paper', 'v', item) for item in [severe, soft]]
    report = run_batch(tasks, service, retry_integrity=True, progress=lambda _: None)
    assert (report['cached'], report['translated'], report['retried_warnings']) == (1, 1, 1)
    assert len(calls) == 1


def test_literal_links_cannot_be_changed_while_numeric_markers_remain_valid():
    segment = render_markdown('See https://example.org/paper/2026?q=1.', '/asset', translation_slots=True).segments[0]
    assert not segment.integrity_warnings(segment.text)
    assert '链接' in ' '.join(segment.integrity_warnings(segment.text.replace('example.org', 'other.invalid')))


def test_link_extraction_stops_at_protected_line_breaks_and_prose_footnotes_remain_visible():
    segment = render_markdown('Available at https://example.org/project.  \n19We describe the study.', '/asset', translation_slots=True).segments[0]
    assert segment.literal_urls() == ('https://example.org/project',)
    assert 'We describe' in segment.text
    assert '19We' not in [value for _, value in segment.protected]


def test_explicit_source_gap_notice_is_visible_and_does_not_trigger_a_hallucinated_repair():
    segment = render_markdown('Where is the set of languages.', '/asset', translation_slots=True).segments[0]
    text = '其中，〔解析文本变量缺失〕是语言集合。'
    assert segment.warnings(text) == ['解析文本缺少变量，请对照 PDF']
    assert not segment.integrity_warnings(text)
    assert 'translation-warning' in segment.render_translation(text)
