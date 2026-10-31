"""Direct, bounded lookup of official bibliographic identifiers."""
from __future__ import annotations

from datetime import datetime, timezone
import html
from html.parser import HTMLParser
import re
import unicodedata
from urllib.parse import quote

import httpx

from .service import EnrichmentResult, _acl_id, normalize_doi

_DOI = re.compile(r'10\.\d{4,9}/[A-Za-z0-9._;()/:+-]{1,240}', re.I)


class _CitationMeta(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.values = {}

    def handle_starttag(self, tag, attrs):
        if tag.casefold() != 'meta':
            return
        attrs = dict(attrs)
        name = (attrs.get('name') or '').casefold()
        value = attrs.get('content') or ''
        if name.startswith('citation_') and value:
            value = html.unescape(re.sub(r'<[^>]*>', '', value))
            value = ' '.join(''.join(c for c in value if unicodedata.category(c) != 'Cf').split())
            if value:
                self.values.setdefault(name, []).append(value)


class ACLClient:
    """Read publisher citation metadata, independently of page navigation."""

    def __init__(self, *, transport: httpx.BaseTransport | None = None):
        self.transport = transport

    def lookup(self, url: str) -> EnrichmentResult:
        identifier = _acl_id(url)
        base = {'source_domain': 'aclanthology.org', 'source_url': url,
                'title_evidence': 'citation_title', 'message': ''}
        if not identifier:
            return EnrichmentResult('not_found', None, {}, {**base, 'message': '不是 ACL 论文地址'})
        url = f'https://aclanthology.org/{identifier}/'
        base['source_url'] = url
        try:
            with httpx.Client(timeout=httpx.Timeout(10, connect=5), transport=self.transport,
                              trust_env=False, follow_redirects=False) as client:
                with client.stream('GET', url) as response:
                    response.raise_for_status()
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        content.extend(chunk)
                        if len(content) > 2 * 1024 * 1024:
                            raise ValueError('ACL 页面超过大小限制')
            parser = _CitationMeta()
            parser.feed(content.decode('utf-8'))
        except (httpx.HTTPError, ValueError):
            return EnrichmentResult('unavailable', None, {}, {**base, 'message': 'ACL 官方结构化信息暂时无法读取'})
        values = parser.values
        titles = list(dict.fromkeys(values.get('citation_title', [])))
        if len(titles) != 1:
            return EnrichmentResult('ambiguous', None, {}, {
                **base, 'reason': 'extraction_incomplete', 'message': 'ACL 官方标题字段缺失或不唯一'})
        def first(key):
            return (values.get(key) or [''])[0]
        pdf = first('citation_pdf_url')
        doi = normalize_doi(first('citation_doi'))
        if ((pdf and _acl_id(pdf) != identifier) or
                (doi.startswith('10.18653/v1/') and doi.split('/', 2)[-1] != identifier)):
            return EnrichmentResult('ambiguous', None, {}, {
                **base, 'reason': 'extraction_incomplete', 'message': 'ACL 页面自身的论文标识字段不一致'})
        bibliography = {'canonical_title': titles[0], 'acl_anthology_id': identifier,
                        'official_url': url, 'venue_type': 'conference'}
        authors = list(dict.fromkeys(values.get('citation_author', [])))
        if authors:
            bibliography['authors'] = authors
        venue = first('citation_conference_title') or first('citation_journal_title')
        if venue:
            bibliography['venue'] = venue
        if first('citation_journal_title') and not first('citation_conference_title'):
            bibliography['venue_type'] = 'journal'
        date = first('citation_publication_date').replace('/', '-')
        if re.fullmatch(r'\d{4}(?:-\d{2}){0,2}', date):
            try:
                numbers = [int(n) for n in date.split('-')]
                datetime(*(numbers + [1] * (3 - len(numbers))))
                bibliography['publication_date'] = date
            except ValueError:
                pass
        if doi and _DOI.fullmatch(doi):
            bibliography['doi'] = doi
        if pdf:
            bibliography['official_pdf_url'] = f'https://aclanthology.org/{identifier}.pdf'
        return EnrichmentResult('verified', 1.0, bibliography, base)


class CrossrefClient:
    """Resolve a DOI from Crossref; its result still needs local identity checks."""

    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        self.transport = transport

    def lookup(self, doi: str) -> EnrichmentResult:
        doi = normalize_doi(doi)
        base = {'source_url': None, 'source_domain': 'api.crossref.org', 'message': ''}
        if not _DOI.fullmatch(doi) or doi.casefold().startswith('10.48550/arxiv.'):
            return EnrichmentResult('not_found', None, {}, {**base, 'message': '不是可查询的 Crossref DOI'})
        url = 'https://api.crossref.org/works/' + quote(doi, safe='')
        try:
            with httpx.Client(timeout=10, transport=self.transport, follow_redirects=False,
                              headers={'User-Agent': 'paper-library/1.0 (bibliographic verification)'}) as client:
                response = client.get(url)
            if response.status_code == 404:
                return EnrichmentResult('not_found', None, {}, {**base, 'message': 'Crossref 无此 DOI'})
            response.raise_for_status()
            data = response.json().get('message')
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            return EnrichmentResult('unavailable', None, {}, {**base, 'message': 'Crossref 暂时无法查询'})
        if not isinstance(data, dict):
            return EnrichmentResult('not_found', None, {}, {**base, 'message': 'Crossref 记录格式不可用'})
        recorded_doi = data.get('DOI')
        if not isinstance(recorded_doi, str) or recorded_doi.casefold() != doi.casefold():
            return EnrichmentResult('conflict', None, {}, {**base, 'message': 'Crossref 返回了不同的 DOI'})
        title = _first(data.get('title'))
        if not title:
            return EnrichmentResult('not_found', None, {}, {**base, 'message': 'Crossref 记录缺少标题'})
        title = html.unescape(re.sub(r'<[^>]+>', '', title)).strip()
        bibliography: dict[str, object] = {
            'canonical_title': title,
            'doi': recorded_doi,
            'official_url': f'https://doi.org/{quote(recorded_doi, safe="/")}',
        }
        venue = _first(data.get('container-title'))
        if venue:
            bibliography['venue'] = html.unescape(re.sub(r'<[^>]+>', '', venue)).strip()
        kind = data.get('type')
        if kind == 'proceedings-article':
            bibliography['venue_type'] = 'conference'
        elif kind == 'journal-article':
            bibliography['venue_type'] = 'journal'
        authors = []
        for author in data.get('author') or []:
            if isinstance(author, dict):
                name = ' '.join(author[part].strip() for part in ('given', 'family')
                                if isinstance(author.get(part), str)).strip()
                if not name and isinstance(author.get('name'), str):
                    name = author['name'].strip()
                if name:
                    authors.append(name)
        if authors:
            bibliography['authors'] = authors
        for key in ('published', 'published-online', 'published-print', 'issued'):
            value = data.get(key)
            parts = value.get('date-parts') if isinstance(value, dict) else None
            if isinstance(parts, list) and parts and isinstance(parts[0], list):
                numbers = parts[0]
                if numbers and all(type(number) is int for number in numbers[:3]):
                    try:
                        datetime(*(numbers[:3] + [1] * max(0, 3 - len(numbers))))
                        date = f'{numbers[0]:04d}'
                        if len(numbers) > 1:
                            date += f'-{numbers[1]:02d}'
                        if len(numbers) > 2:
                            date += f'-{numbers[2]:02d}'
                        bibliography['publication_date'] = date
                        break
                    except (IndexError, ValueError):
                        continue
        return EnrichmentResult('verified', 1.0, bibliography,
                                {**base, 'source_url': url, 'searched_at': datetime.now(timezone.utc).isoformat()})


def _first(value: object) -> str:
    if isinstance(value, list) and value and isinstance(value[0], str):
        return value[0]
    return value if isinstance(value, str) else ''
