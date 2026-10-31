import json

import httpx

from paper_library.metadata.identifiers import CrossrefClient


def test_crossref_doi_lookup_returns_official_bibliography():
    seen = []
    def respond(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={'message': {
            'DOI': '10.1109/SP63933.2026.00095',
            'title': ['MetaBreak: Jailbreaking Online LLM Services via Special Token Manipulation'],
            'author': [{'given': 'Wentian', 'family': 'Zhu'}, {'given': 'Zhen', 'family': 'Xiang'}],
            'container-title': ['2026 IEEE Symposium on Security and Privacy (SP)'],
            'type': 'proceedings-article',
            'published': {'date-parts': [[2026, 5, 18]]},
        }})
    client = CrossrefClient(transport=httpx.MockTransport(respond))
    result = client.lookup('10.1109/SP63933.2026.00095')
    assert result.status == 'verified'
    assert result.bibliography['canonical_title'].startswith('MetaBreak:')
    assert result.bibliography['authors'] == ['Wentian Zhu', 'Zhen Xiang']
    assert result.bibliography['venue_type'] == 'conference'
    assert result.bibliography['publication_date'] == '2026-05-18'
    assert result.enrichment['source_domain'] == 'api.crossref.org'
    assert seen == ['https://api.crossref.org/works/10.1109%2FSP63933.2026.00095']


def test_crossref_rejects_mismatched_identifier_and_invalid_input():
    calls = []
    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={'message': {'DOI': '10.1000/other', 'title': ['Other Paper']}})
    client = CrossrefClient(transport=httpx.MockTransport(respond))
    assert client.lookup('10.1109/SP63933.2026.00095').status == 'conflict'
    assert client.lookup('https://evil.example/metadata').status == 'not_found'
    assert len(calls) == 1
