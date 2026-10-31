import json

import pytest

from paper_library.catalog import PaperPaths
from paper_library.ingest.service import refresh_metadata
from paper_library.metadata.repair import verify_bibliography, title_clues
from test_ingest_service import write_library
from test_metadata import FakeGateway, response


TITLE = 'Vulnerability of LLMs to Vertically Aligned Text Manipulations'
URL = 'https://aclanthology.org/2025.acl-long.1/'


def test_heading_boilerplate_is_excluded_from_title_clues():
    title = 'MetaBreak: Jailbreaking Online LLM Services via Special Token Manipulation'
    clues = title_clues('', '2510.10271v2.pdf',
                        f'# {title} Disclaimer: This paper contains unfiltered content.\n\nAuthor')
    assert ('markdown', title) in clues
    assert not any('Disclaimer' in value for _, value in clues)


def test_split_title_and_conference_filename_supply_complete_clues():
    first = 'Quantifying Language Models Sensitivity or:'
    second = 'How I learned to start worrying about prompt formatting'
    full = first + ' ' + second
    clues = title_clues('filename', 'ICLR-2024-' + full.replace(' ', '-') + '-Paper-Conference.pdf',
                        f'# {first}\n\n# {second}\n\nAuthors\n\n# ABSTRACT')
    assert ('markdown', full) in clues
    assert ('filename', full) in clues


def test_title_does_not_join_unrelated_heading():
    clues = title_clues('', '', '# A Paper Title\n\n# Jane Doe')
    assert ('markdown', 'A Paper Title') in clues
    assert ('markdown', 'A Paper Title Jane Doe') not in clues


def test_arxiv_truncated_result_uses_title_field_and_extracts_abstract_page():
    url = 'https://arxiv.org/abs/2310.11324'
    client = FakeGateway(response(200, {}, 'ready'),
        response(200, {'results': [{'title': '[2310.11324] Vulnerability of ...',
            'url': 'https://arxiv.org/html/2310.11324v2', 'content': f'# Title:{TITLE}\nAbstract text'}]}, 'search'),
        response(200, {'results': [{'url': url, 'raw_content': f'# Title:{TITLE}'}]}, 'extract'))
    assert verify(client).status == 'verified'


def test_arxiv_filename_uses_official_page_when_search_results_are_truncated(monkeypatch):
    from paper_library.metadata.service import GatewayResponse
    title = 'MetaBreak: Jailbreaking Online LLM Services via Special Token Manipulation'
    url = 'https://arxiv.org/abs/2510.10271'
    content = (
        f'# Title:{title}\n'
        '| Journal\u00a0reference: | Proceedings of the 2026 IEEE Symposium on Security and Privacy (SP), pp. 98-117, IEEE Computer Society, 2026 |\n'
        '| Related DOI: | <https://doi.org/10.1109/SP63933.2026.00095> |\n'
    )
    monkeypatch.setattr('paper_library.metadata.repair.pdf_frontmatter', lambda _: (title, ''))
    monkeypatch.setattr('paper_library.metadata.queries.generate_search_plan', lambda *a, **kw: {'status': 'disabled'})

    class Gateway:
        def __init__(self): self.extracted_urls = []
        def ready(self): return GatewayResponse(200, {})
        def search(self, query): return GatewayResponse(200, {'results': []})
        def extract(self, urls):
            self.extracted_urls.extend(urls)
            return GatewayResponse(200, {'results': [{'url': url, 'raw_content': content}]})

    gateway = Gateway()
    result = verify_bibliography('2510.10271v2', '', '', '2510.10271v2.pdf',
        '# ' + title + ' Disclaimer: extra PDF text\n\nAbstract', gateway, pdf='paper.pdf')
    assert result.status == 'verified'
    assert result.bibliography['canonical_title'] == title
    assert result.bibliography['venue_type'] == 'conference'
    assert result.bibliography['publication_date'] == '2026'
    assert result.bibliography['doi'] == '10.1109/SP63933.2026.00095'
    assert result.enrichment['matched_clues'] == ['markdown', 'pdf']
    assert gateway.extracted_urls == [url]


def test_direct_doi_still_requires_local_title_agreement(monkeypatch):
    from paper_library.metadata.service import EnrichmentResult, GatewayResponse
    title = 'A Valid Paper About Language Models'
    monkeypatch.setattr('paper_library.metadata.queries.generate_search_plan', lambda *a, **kw: {'status': 'disabled'})
    class Official:
        def lookup(self, doi):
            assert doi == '10.1000/valid-paper'
            return EnrichmentResult('verified', 1.0,
                {'canonical_title': title, 'doi': doi, 'venue': 'Example Journal'},
                {'source_url': 'https://api.crossref.org/works/10.1000%2Fvalid-paper',
                 'source_domain': 'api.crossref.org', 'message': ''})
    class Gateway:
        def ready(self): return GatewayResponse(200, {})
        def search(self, query): return GatewayResponse(200, {'results': []})
        def extract(self, urls): return GatewayResponse(200, {'results': []})
    source = f'# {title}\n\nDOI: 10.1000/valid-paper\n\nAbstract'
    result = verify_bibliography('filename', '', '', 'unrelated.pdf', source, Gateway(), official_lookup=Official())
    assert result.status == 'verified'
    assert result.enrichment['matched_clues'] == ['markdown']
    mismatch = verify_bibliography('filename', '', '', 'unrelated.pdf',
        '# A Different Paper\n\nDOI: 10.1000/valid-paper\n\nAbstract',
        Gateway(), official_lookup=Official())
    assert mismatch.status == 'conflict'
    assert mismatch.bibliography == {}


def test_doi_in_pdf_frontmatter_is_checked_even_with_parsed_title(monkeypatch):
    from paper_library.metadata.service import EnrichmentResult, GatewayResponse
    title = 'A Valid Paper About Language Models'
    doi = '10.1000/pdf-frontmatter'
    monkeypatch.setattr('paper_library.metadata.repair.pdf_frontmatter',
                        lambda _: (title, f'{title}\nAuthors\nDOI: {doi}\nAbstract'))
    monkeypatch.setattr('paper_library.metadata.queries.generate_search_plan',
                        lambda *a, **kw: {'status': 'disabled'})

    class Official:
        def lookup(self, value):
            assert value == doi
            return EnrichmentResult('verified', 1.0,
                {'canonical_title': title, 'doi': doi},
                {'source_url': 'https://api.crossref.org/works/10.1000%2Fpdf-frontmatter',
                 'source_domain': 'api.crossref.org'})

    class Gateway:
        def ready(self): return GatewayResponse(200, {})
        def search(self, query): return GatewayResponse(200, {'results': []})
        def extract(self, urls): return GatewayResponse(200, {'results': []})

    result = verify_bibliography('unrelated', '', '', 'paper.pdf',
        f'# {title}\n\nAuthors\n\nAbstract', Gateway(), pdf='paper.pdf', official_lookup=Official())
    assert result.status == 'verified'
    assert result.enrichment['identifier_clues']['doi'] == doi


def test_unavailable_doi_lookup_falls_back_to_search(monkeypatch):
    from paper_library.metadata.service import EnrichmentResult
    monkeypatch.setattr('paper_library.metadata.queries.generate_search_plan', lambda *a, **kw: {'status': 'disabled'})

    class Official:
        def lookup(self, doi):
            return EnrichmentResult('unavailable', None, {}, {'message': 'Crossref temporarily unavailable'})

    source = f'# {TITLE}\n\nDOI: 10.1000/example\n\nAbstract'
    result = verify_bibliography('unrelated', '2025', 'ACL', 'unrelated.pdf',
                                 source, gateway(), official_lookup=Official())
    assert result.status == 'verified'
    assert result.bibliography['canonical_title'] == TITLE
    assert result.enrichment['attempts'][0]['status'] == 'unavailable'


def test_near_title_needs_matching_first_author_for_direct_doi(monkeypatch):
    from paper_library.metadata.service import EnrichmentResult, GatewayResponse
    title = 'Quantifying Language Models Sensitivity to Spurious Features in Prompt Design'
    local = 'Quantifying Language Model Sensitivity to Spurious Features in Prompt Design'
    doi = '10.1000/near-title'
    monkeypatch.setattr('paper_library.metadata.queries.generate_search_plan', lambda *a, **kw: {'status': 'disabled'})
    class Official:
        def lookup(self, value):
            return EnrichmentResult('verified', 1.0,
                {'canonical_title': title, 'doi': value, 'authors': ['Melanie Sclar']},
                {'source_url': 'https://api.crossref.org/works/10.1000%2Fnear-title',
                 'source_domain': 'api.crossref.org', 'message': ''})
    class Gateway:
        def ready(self): return GatewayResponse(200, {})
        def search(self, query): return GatewayResponse(200, {'results': []})
        def extract(self, urls): return GatewayResponse(200, {'results': []})
    matched = verify_bibliography('filename', '', '', 'paper.pdf',
        f'# {local}\n\nMelanie Sclar\nDOI: {doi}\n\nAbstract',
        Gateway(), official_lookup=Official())
    assert matched.status == 'verified'
    assert 'markdown' in matched.enrichment['matched_clues']
    mismatch = verify_bibliography('filename', '', '', 'paper.pdf',
        f'# {local}\n\nOther Author\nDOI: {doi}\n\nAbstract',
        Gateway(), official_lookup=Official())
    assert mismatch.status == 'conflict'


def test_unrelated_arxiv_filename_cannot_override_pdf_title(monkeypatch):
    from paper_library.metadata.service import GatewayResponse
    monkeypatch.setattr('paper_library.metadata.repair.pdf_frontmatter', lambda _: ('A Different Study', ''))
    monkeypatch.setattr('paper_library.metadata.queries.generate_search_plan', lambda *a, **kw: {'status': 'disabled'})
    class Gateway:
        def ready(self): return GatewayResponse(200, {})
        def search(self, query): return GatewayResponse(200, {'results': []})
        def extract(self, urls):
            return GatewayResponse(200, {'results': [{'url': urls[0], 'raw_content': '# Title:MetaBreak: Jailbreaking Online LLM Services via Special Token Manipulation'}]})
    result = verify_bibliography('unrelated', '', '', '2510.10271v2.pdf',
        '# A Different Study\n\nAbstract', Gateway(), pdf='paper.pdf')
    assert result.status == 'conflict'


def test_acl_filename_extracts_official_record_without_search(monkeypatch):
    from paper_library.metadata.service import GatewayResponse
    title = 'A Paper With A Clear Local Title'
    url = 'https://aclanthology.org/2024.findings-emnlp.692/'
    monkeypatch.setattr('paper_library.metadata.queries.generate_search_plan', lambda *a, **kw: {'status': 'disabled'})
    class Gateway:
        def __init__(self): self.urls = []
        def ready(self): return GatewayResponse(200, {})
        def search(self, query): return GatewayResponse(200, {'results': []})
        def extract(self, urls):
            self.urls.extend(urls)
            return GatewayResponse(200, {'results': [{'url': url,
                'raw_content': f'# {title}\nAnthology ID:\n:   2024.findings-emnlp.692\nYear:\n:   2024'}]})
    gateway = Gateway()
    result = verify_bibliography('2024.findings-emnlp.692', '', '',
        '2024.findings-emnlp.692.pdf', '# ' + title, gateway)
    assert result.status == 'verified'
    assert result.bibliography['acl_anthology_id'] == '2024.findings-emnlp.692'
    assert gateway.urls == [url]


def test_arxiv_citation_in_snippet_does_not_replace_candidate_title():
    from paper_library.metadata.service import _rank_candidates
    results = {'results': [{'title': 'A Different Paper', 'url': 'https://arxiv.org/abs/2310.11324',
                           'content': f'This paper cites {TITLE}'}]}
    assert not _rank_candidates(TITLE, '', '', results)


def test_refresh_preserves_split_title_when_complete_title_matches(tmp_path, monkeypatch):
    repository, library, paths = fixture_paper(tmp_path, monkeypatch)
    paths.markdown.write_text('# Vulnerability of LLMs to\n\n# Vertically Aligned Text Manipulations\n\nAuthors')
    original = paths.markdown.read_bytes()
    refresh_metadata(repository, 'whitespace', 'ws-p0001', gateway=gateway())
    assert repository.load('whitespace').find('ws-p0001').title == TITLE
    assert paths.markdown.read_bytes() == original


def gateway(content=None):
    return FakeGateway(
        response(200, {'status': 'ready'}, 'ready'),
        response(200, {'results': [{'title': TITLE, 'url': URL, 'content': 'ACL 2025'}]}, 'search'),
        response(200, {'results': [{'url': URL, 'raw_content': content if content is not None else f'# {TITLE}\nAuthors: Jane Doe\nPublication date: 2025-07-27\nDOI: 10.1000/example\n'}]}, 'extract'),
    )


def verify(client, **kwargs):
    return verify_bibliography('of LLMs to Vertically Aligned Text Manipulations', '2025', 'ACL',
                              f'2025-ACL-{TITLE}.pdf', f'# {TITLE}\n\nAbstract', client, **kwargs)


def test_full_local_heading_recovers_truncated_catalog_title():
    result = verify(gateway())
    assert result.status == 'verified'
    assert result.bibliography['canonical_title'] == TITLE
    assert 'markdown' in result.enrichment['matched_clues']


def test_missing_extracted_title_is_not_replaced_by_search_title():
    assert verify(gateway('Authors: Jane Doe')).status == 'ambiguous'


def test_conflicting_identifier_prevents_repair():
    assert verify(gateway(), known={'doi': '10.1000/other'}).status == 'conflict'


def test_acl_pdf_result_extracts_landing_page():
    client = gateway()
    client.search_response.payload['results'][0]['url'] = URL.rstrip('/') + '.pdf'
    result = verify(client)
    assert result.status == 'verified'
    assert result.bibliography['official_url'] == URL
    assert result.bibliography['acl_anthology_id'] == '2025.acl-long.1'


def fixture_paper(tmp_path, monkeypatch):
    # The filename alone must not authorize replacing an unrelated heading.
    monkeypatch.setattr('paper_library.metadata.repair.pdf_frontmatter', lambda path: (TITLE, ''))
    pdf = tmp_path / 'input.pdf'
    pdf.write_bytes(b'%PDF-test')
    repository, library = write_library(tmp_path / 'libraries', pdf)
    paths = PaperPaths.for_id(library, 'ws-p0001')
    paths.metadata.write_text(json.dumps({'original_filename': f'2025-ACL-{TITLE}.pdf', 'bibliography': {}}))
    return repository, library, paths


def test_refresh_synchronizes_catalog_metadata_heading_and_keeps_backup(tmp_path, monkeypatch):
    repository, library, paths = fixture_paper(tmp_path, monkeypatch)
    original = paths.markdown.read_bytes()
    refresh_metadata(repository, 'whitespace', 'ws-p0001', gateway=gateway())
    assert repository.load('whitespace').find('ws-p0001').title == TITLE
    assert json.loads(paths.metadata.read_text())['title'] == TITLE
    # An unrelated short heading is deliberately left intact.
    assert paths.markdown.read_bytes() == original
    assert next((paths.root / 'metadata-repairs').glob('*/article.md')).read_bytes() == original


def test_ambiguous_refresh_preserves_existing_data(tmp_path, monkeypatch):
    repository, library, paths = fixture_paper(tmp_path, monkeypatch)
    files = [paths.markdown, paths.metadata, library / 'catalog.tsv']
    originals = [p.read_bytes() for p in files]
    refresh_metadata(repository, 'whitespace', 'ws-p0001', gateway=gateway('No title'))
    assert [p.read_bytes() for p in files] == originals


def test_failed_catalog_publish_restores_other_files(tmp_path, monkeypatch):
    repository, library, paths = fixture_paper(tmp_path, monkeypatch)
    files = [paths.markdown, paths.metadata, library / 'catalog.tsv']
    originals = [p.read_bytes() for p in files]
    import os
    replace = os.replace

    def fail_catalog(source, target):
        if target == library / 'catalog.tsv':
            raise OSError('simulated publication failure')
        return replace(source, target)

    monkeypatch.setattr('paper_library.ingest.service.os.replace', fail_catalog)
    with pytest.raises(OSError, match='simulated'):
        refresh_metadata(repository, 'whitespace', 'ws-p0001', gateway=gateway())
    assert [p.read_bytes() for p in files] == originals


def test_nature_truncated_search_title_verified_against_full_official_title():
    title = 'Evaluating large language models for accuracy incentivizes hallucinations'
    url = 'https://www.nature.com/articles/s41586-026-10549-w'
    client = FakeGateway(response(200, {}, "ready"),
        response(200, {'results': [{'title': 'Evaluating large language models for accuracy incentivizes ... - Nature', 'url': url}]}, 'search'),
        response(200, {'results': [{'url': url, 'raw_content': f'# {title}\nPublished: 22 April 2026\nDOI: https://doi.org/10.1038/s41586-026-10549-w'}]}, 'extract'))
    result = verify_bibliography('s41586-026-10549-w', '', '', 's41586-026-10549-w.pdf',
        f'# {title}\nhttps://doi.org/10.1038/s41586-026-10549-w', client)
    assert result.status == 'verified'
    assert result.bibliography['venue'] == 'Nature'
    assert result.bibliography['publication_date'] == '2026-04-22'


@pytest.mark.parametrize('pdf_path', [
    'anthology-files/anthology-files/pdf/acl/2025.acl-long.1.pdf',
    'anthology-files/pdf/acl/2025.acl-long.1.pdf',
    '2025.acl-long.1.pdf',
])
def test_acl_mirror_pdf_and_landing_page_are_one_candidate(pdf_path):
    client = gateway()
    client.search_response.payload['results'].append({
        'title': TITLE, 'url': 'https://aclanthology.org/' + pdf_path, 'content': 'ACL 2025'})
    result = verify(client)
    assert result.status == 'verified'
    assert result.bibliography['official_url'] == URL
    assert result.bibliography['acl_anthology_id'] == '2025.acl-long.1'


def test_different_acl_papers_with_same_title_still_require_confirmation():
    client = gateway()
    client.search_response.payload['results'].append({
        'title': TITLE, 'url': 'https://aclanthology.org/2025.acl-long.2/', 'content': 'ACL 2025'})
    assert verify(client).status == 'ambiguous'


def test_deep_acl_pdf_without_landing_result_uses_official_landing_page():
    client = gateway()
    client.search_response.payload['results'][0]['url'] = 'https://aclanthology.org/anthology-files/anthology-files/pdf/acl/2025.acl-long.1.pdf'
    result = verify(client)
    assert result.status == 'verified'
    assert result.bibliography['official_url'] == URL


def test_iclr_pdf_publication_hint_prefers_official_proceedings_over_preprint(monkeypatch):
    from paper_library.metadata.service import GatewayResponse
    title = "Quantifying Language Models' Sensitivity to Spurious Features in Prompt Design"
    official = 'https://proceedings.iclr.cc/paper_files/paper/2024/hash/6c0e99d736da621403018ca7b32b1a4d-Abstract-Conference.html'
    preprint = 'https://arxiv.org/abs/2310.11324'
    monkeypatch.setattr('paper_library.metadata.repair.pdf_frontmatter', lambda _: (
        '', 'Published as a conference paper at ICLR 2024\n' + title))

    class Gateway:
        def __init__(self): self.queries = []
        def ready(self): return GatewayResponse(200, {})
        def search(self, query):
            self.queries.append(query)
            if 'proceedings.iclr.cc' in query:
                return GatewayResponse(200, {'results': [{'title': title, 'url': official, 'content': 'ICLR 2024'}]})
            return GatewayResponse(200, {'results': [{'title': title, 'url': preprint, 'content': '2023'}]})
        def extract(self, urls):
            url = urls[0]
            content = (f'# {title}\n\nMelanie Sclar, Yejin Choi, Yulia Tsvetkov, Alane Suhr\n\n'
                       '[International Conference on Learning Representations 2024 (ICLR 2024)](/paper_files/paper/2024) Conference\n\n'
                       '[Paper](/paper_files/paper/2024/file/6c0e99d736da621403018ca7b32b1a4d-Paper-Conference.pdf)')
            return GatewayResponse(200, {'results': [{'url': url, 'raw_content': content}]})

    gateway = Gateway()
    result = verify_bibliography(title, '2023', 'arXiv', 'unrelated.pdf',
                                 '# ' + title, gateway, pdf='paper.pdf',
                                 known={'arxiv_id': '2310.11324', 'doi': '10.48550/arXiv.2310.11324'})
    assert result.status == 'verified'
    assert result.bibliography['venue_type'] == 'conference'
    assert result.bibliography['publication_date'] == '2024'
    assert result.bibliography['authors'][0] == 'Melanie Sclar'
    assert result.bibliography['official_url'] == official
    assert gateway.queries[0].startswith('site:proceedings.iclr.cc/')


def test_official_publication_drops_inherited_arxiv_doi_from_citation():
    from paper_library.ingest.service import _merge_bibliography
    merged = _merge_bibliography(
        {'venue_type': 'preprint', 'doi': '10.48550/arXiv.2310.11324', 'arxiv_id': '2310.11324'},
        {'venue_type': 'conference', 'official_url': 'https://proceedings.iclr.cc/paper'},
    )
    assert merged['venue_type'] == 'conference'
    assert merged['arxiv_id'] == '2310.11324'
    assert 'doi' not in merged
