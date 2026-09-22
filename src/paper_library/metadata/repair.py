"""Verify bibliographic identities using independent local title clues."""
import re
from .service import EnrichmentResult, _normalize, _TRUSTED_DOMAINS, enrich_bibliography


def title_clues(title, filename, markdown):
    clues = []
    heading = re.search(r'(?m)^#\s+(.+?)\s*$', markdown[:12000])
    if heading and heading.group(1).casefold() not in {'abstract', 'introduction', 'references'}:
        clues.append(('markdown', heading.group(1).strip()))
    filename = filename.replace('\\', '/').rsplit('/', 1)[-1]
    name = re.sub(r'(?i)\.pdf$', '', filename)
    name = re.sub(r'^\d{4}-[A-Za-z0-9]+-', '', name)
    if name and name.casefold() != 'source':
        clues.append(('filename', name))
    clues.append(('catalog', title))
    return clues


def pdf_frontmatter(path):
    """PDF evidence is optional when the PDF reader is not installed."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        text = reader.pages[0].extract_text()[:16000] if reader.pages else ''
        title = str((reader.metadata or {}).get('/Title') or '').strip()
        return title, text
    except Exception:
        return '', ''


def verify_bibliography(title, year, venue, filename, markdown, gateway, *, pdf=None, known=None):
    clues = title_clues(title, filename, markdown)
    pdf_title, first_page = pdf_frontmatter(pdf) if pdf else ('', '')
    if len(pdf_title.split()) >= 4:
        clues.insert(0, ('pdf', pdf_title))
    known = known or {}
    identifiers = {key: str(known[key]) for key in ('doi', 'arxiv_id', 'acl_anthology_id') if known.get(key)}
    if 'acl_anthology_id' in identifiers:
        identifiers['acl_anthology_id'] = identifiers['acl_anthology_id'].removesuffix('.pdf')
    front = re.split(r'(?i)\babstract\b', first_page or markdown[:4000], maxsplit=1)[0]
    doi = re.search(r'10\.\d{4,9}/[A-Za-z0-9./_-]+', front)
    arxiv = re.search(r'(?i)arxiv\s*:?\s*(\d{4}\.\d{4,5})', front)
    if doi:
        identifiers.setdefault('doi', doi.group().rstrip('.'))
    if arxiv:
        identifiers.setdefault('arxiv_id', arxiv.group(1))

    class EvidenceGateway:
        extracted = ''

        def ready(self):
            return gateway.ready()

        def search(self, query):
            return gateway.search(query)

        def extract(self, urls):
            response = gateway.extract(urls)
            self.extracted = '\n'.join(str(item.get('raw_content') or '') for item in response.payload.get('results', []) if item.get('url') in urls)
            return response

    evidence = EvidenceGateway()
    attempts = []
    seen = set()
    for origin, query_title in clues:
        normalized = _normalize(query_title)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result = enrich_bibliography(query_title, year, venue, evidence)
        attempts.append({'origin': origin, 'query': query_title, 'status': result.status})
        if result.status == 'unavailable':
            return EnrichmentResult(result.status, result.confidence, {}, {
                **result.enrichment, 'attempts': attempts, 'local_clues': clues})
        if result.status != 'verified':
            continue
        if result.enrichment.get('source_domain') not in _TRUSTED_DOMAINS:
            continue
        bibliography = result.bibliography
        canonical = str(bibliography.get('canonical_title') or '')
        # A search match alone must not authorize overwriting local metadata.
        # Require the extracted official title to agree with a local primary clue.
        matches = [source for source, value in clues
                   if _normalize(value) == _normalize(canonical)]
        if not any(source in {'markdown', 'filename', 'pdf'} for source in matches):
            continue
        if _normalize(canonical) not in _normalize(evidence.extracted):
            continue
        if any(bibliography.get(key) and str(bibliography[key]).casefold() != value.casefold()
               for key, value in identifiers.items()):
            continue
        return EnrichmentResult('verified', result.confidence, bibliography, {
            **result.enrichment, 'attempts': attempts, 'local_clues': clues,
            'matched_clues': matches, 'identifier_clues': identifiers,
        })
    return EnrichmentResult('ambiguous', None, {}, {
        'message': '官方标题与本地文件名或正文标题未能交叉确认，保留原信息',
        'attempts': attempts, 'local_clues': clues,
    })


def repaired_short_name(short_name, canonical):
    # A word split out of a filename is not a separate acronym.
    if short_name and _normalize(canonical).startswith(_normalize(short_name) + ' '):
        return ''
    return short_name
