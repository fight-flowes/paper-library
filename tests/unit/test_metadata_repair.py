import json

import pytest

from paper_library.catalog import PaperPaths
from paper_library.ingest.service import refresh_metadata
from paper_library.metadata.repair import verify_bibliography
from test_ingest_service import write_library
from test_metadata import FakeGateway, response


TITLE = 'Vulnerability of LLMs to Vertically Aligned Text Manipulations'
URL = 'https://aclanthology.org/2025.acl-long.1/'


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
    assert verify(gateway(), known={'doi': '10.1000/other'}).status == 'ambiguous'


def test_acl_pdf_result_extracts_landing_page():
    client = gateway()
    client.search_response.payload['results'][0]['url'] = URL.rstrip('/') + '.pdf'
    result = verify(client)
    assert result.status == 'verified'
    assert result.bibliography['official_url'] == URL
    assert result.bibliography['acl_anthology_id'] == '2025.acl-long.1'


def fixture_paper(tmp_path, monkeypatch):
    monkeypatch.setattr('paper_library.metadata.repair.pdf_frontmatter', lambda path: ('', ''))
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
