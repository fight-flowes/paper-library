"""Verify bibliographic identities using independent local title clues."""
import re
import json
from pathlib import Path
from difflib import SequenceMatcher
from paper_library.structure.frontmatter import primary_article, source_headings, section_label
from .service import EnrichmentResult, _extract_bibliography, _normalize, _TRUSTED_DOMAINS, enrich_bibliography, normalize_doi


def title_clues(title, filename, markdown):
    clues = []
    article = primary_article(markdown)
    headings = source_headings(markdown)
    heading = headings[0].title if headings else ''
    split_title = leading_split_title(markdown)
    if article:
        clues.append(('markdown', _clean_heading(article.title)))
    elif heading and section_label(heading) not in {'abstract', 'introduction', 'references', 'research briefing', 'article'}:
        if split_title and re.search(r'(?i)(?:[:：]|\b(?:or|to|of|and|in|for))\s*$', heading):
            clues.append(('markdown', _clean_heading(split_title)))
        clues.append(('markdown', _clean_heading(heading)))
    filename = filename.replace('\\', '/').rsplit('/', 1)[-1]
    name = re.sub(r'(?i)\.pdf$', '', filename)
    name = re.sub(r'^\d{4}-[A-Za-z0-9]+-', '', name)
    name = re.sub(r'(?i)_(?:briefing\s*&\s*si|briefing|si|supplementary(?:[ _-]+(?:information|materials))?)$', '', name)
    if re.match(r'^[A-Za-z][A-Za-z0-9]*-\d{4}-', name):
        name = re.sub(r'^[A-Za-z][A-Za-z0-9]*-\d{4}-', '', name)
        name = re.sub(r'(?i)-Paper-Conference$', '', name).replace('-', ' ')
    if name and name.casefold() != 'source':
        clues.append(('filename', name))
    clues.append(('catalog', title))
    return clues


def _clean_heading(title: str) -> str:
    # MinerU sometimes attaches a separate front-matter label to the title.
    return re.split(r'(?i)\s+(?=(?:Disclaimer|Abstract|Keywords|Copyright):)',
                    title.strip(), maxsplit=1)[0].strip()


def leading_split_title(markdown):
    """Return adjacent opening title lines for comparison with an official title."""
    match = re.match(r'\s*#\s+([^\n]+)\n\s*\n#\s+([^\n]+)', markdown)
    if not match or match[2].strip().casefold() in {'abstract', 'introduction', 'references'}:
        return ''
    return match[1].strip() + ' ' + match[2].strip()


def parsed_display_title(title, markdown):
    """Use a clear parsed title for ID-only filenames, without claiming verification."""
    if not re.fullmatch(r'(?:\d{4}\.[a-z][a-z0-9-]*\.\d+|[A-Z]\d{2}-\d{4}|\d{4}\.\d{4,5}(?:v\d+)?)', title, re.I):
        return title
    headings = source_headings(markdown)
    if headings:
        candidate = _clean_heading(headings[0].title)
        words = _normalize(candidate).split()
        if (4 <= len(words) and len(candidate) <= 500 and not words[0].isdigit()
                and words[0] not in {'abstract', 'introduction', 'references', 'appendix',
                                     'acknowledgements', 'contents', 'supplementary', 'methods', 'results'}):
            return candidate
    return title


def _corroborated_title(local: str, canonical: str, bibliography: dict,
                        frontmatter: str) -> bool:
    left, right = _normalize(local), _normalize(canonical)
    if not left or not right:
        return False
    if left == right:
        return True
    authors = bibliography.get('authors')
    if not isinstance(authors, list) or not authors or not isinstance(authors[0], str):
        return False
    first_author = _normalize(authors[0])
    if len(first_author.split()) < 2 or first_author not in _normalize(frontmatter):
        return False
    shared = set(left.split()) & set(right.split())
    overlap = len(shared) / max(len(set(left.split())), len(set(right.split())))
    return overlap >= 0.85 and SequenceMatcher(None, left, right).ratio() >= 0.9


def pdf_frontmatter(path):
    """Read evidence generated on cicore01; never parse a PDF on this host."""
    try:
        data = json.loads((Path(path).parent / 'pdf-frontmatter.json').read_text(encoding='utf-8'))
        title, text = data.get('title', ''), data.get('text', '')
        if not isinstance(title, str) or not isinstance(text, str):
            return '', ''
        return title[:2000], text[:16000]
    except (OSError, ValueError, AttributeError):
        return '', ''


def verify_bibliography(title, year, venue, filename, markdown, gateway, *, pdf=None, known=None,
                        settings=None, official_lookup=None, acl_lookup=None, cancelled=lambda: False):
    clues = title_clues(title, filename, markdown)
    article = primary_article(markdown)
    pdf_title, first_page = pdf_frontmatter(pdf) if pdf else ('', '')
    if len(pdf_title.split()) >= 4 and not (article and article.combined):
        position = sum(origin == 'markdown' for origin, _ in clues)
        clues.insert(position, ('pdf', pdf_title))
    known = known or {}
    identifiers = {key: str(known[key]) for key in ('doi', 'arxiv_id', 'acl_anthology_id') if known.get(key)}
    if identifiers.get('doi'):
        identifiers['doi'] = normalize_doi(identifiers['doi'])
    if 'acl_anthology_id' in identifiers:
        identifiers['acl_anthology_id'] = identifiers['acl_anthology_id'].removesuffix('.pdf')
    headings = source_headings(markdown)
    local_front = ''.join(markdown.splitlines(keepends=True)[headings[0].start_line - 1:]) if headings else markdown
    front = re.split(r'(?i)\babstract\b', (article.text if article else local_front)[:6000], maxsplit=1)[0]
    pdf_front = re.split(r'(?i)\babstract\b', first_page, maxsplit=1)[0]
    if len(front.strip()) < 20:
        front = pdf_front or front
    # The parsed front matter may omit a DOI visible on the PDF's first page.
    from paper_library.structure.document import build_blocks
    safe_front = '\n'.join(block['text'] for block in build_blocks(front)
                           if block['type'] not in {'fence', 'code_block', 'math_block'})
    identifier_front = safe_front if article and article.combined else safe_front + '\n' + pdf_front
    doi = re.search(r'(?im)^[ \t]*(?:https?://(?:dx\.)?doi\.org/|doi[ \t]*:[ \t]*)'
                    r'(10\.\d{4,9}/[A-Za-z0-9._;()/:+-]+)', identifier_front)
    arxiv = re.search(r'(?i)arxiv\s*:?\s*(\d{4}\.\d{4,5})', identifier_front)
    if article:
        identifiers.setdefault('doi', article.doi)
    elif doi:
        identifiers.setdefault('doi', doi[1].rstrip('.,;'))
    if arxiv:
        identifiers.setdefault('arxiv_id', arxiv.group(1))
    preprint_doi = re.fullmatch(r'10\.48550/arxiv\.(\d{4}\.\d{4,5})', identifiers.get('doi', ''), re.I)
    if preprint_doi:
        identifiers.setdefault('arxiv_id', preprint_doi[1])
        identifiers.pop('doi')

    class EvidenceGateway:
        extracted = ''

        def __init__(self):
            self.extractions = {}

        def ready(self):
            return gateway.ready()

        def search(self, query):
            return gateway.search(query)

        def extract(self, urls):
            key = tuple(urls)
            response = self.extractions.get(key)
            if response is None:
                response = gateway.extract(urls)
                if response.status_code == 200:
                    self.extractions[key] = response
            self.extracted = '\n'.join(str(item.get('raw_content') or '') for item in response.payload.get('results', []) if item.get('url') in urls)
            return response

    evidence = EvidenceGateway()
    from .queries import generate_search_plan
    plan = generate_search_plan(clues, filename, front, year, venue, settings=settings, cancelled=cancelled)
    queries = ([plan['primary'], *plan.get('alternatives', [])] if plan.get('status') == 'generated' else [])
    entries = []
    if official_lookup is not None and identifiers.get('doi') and not identifiers['doi'].casefold().startswith('10.48550/arxiv.'):
        entries.append(('doi', pdf_title or clues[0][1], identifiers['doi']))
    filename_stem = filename.replace('\\', '/').rsplit('/', 1)[-1]
    acl_filename = re.fullmatch(r'((?:\d{4}\.[a-z][a-z0-9-]*\.\d+|[A-Z]\d{2}-\d{4}))\.pdf',
                                filename_stem, re.I)
    acl_id = identifiers.get('acl_anthology_id') or (acl_filename[1] if acl_filename else None)
    if acl_id and re.fullmatch(r'(?:\d{4}\.[a-z][a-z0-9-]*\.\d+|[A-Z]\d{2}-\d{4})', acl_id, re.I):
        if acl_lookup is not None:
            entries.append(('acl-metadata', pdf_title or clues[0][1], f'https://aclanthology.org/{acl_id}/'))
        entries.append(('acl-id', pdf_title or clues[0][1], f'https://aclanthology.org/{acl_id}/'))
    # An exact arXiv filename or an already verified identifier can point to
    # the official record directly. Its extracted title must still match the
    # PDF or parsed text below; filenames alone never authorize a repair.
    filename_id = re.fullmatch(r'(\d{4}\.\d{4,5})(?:v\d+)?\.pdf',
                               filename.replace('\\', '/').rsplit('/', 1)[-1], re.I)
    arxiv_id = identifiers.get('arxiv_id') or (filename_id[1] if filename_id else None)
    if arxiv_id and re.fullmatch(r'\d{4}\.\d{4,5}', arxiv_id):
        entries.append(('arxiv-id', pdf_title or clues[0][1], f'https://arxiv.org/abs/{arxiv_id}'))
    published = re.search(r'(?i)published as a conference paper at ICLR\s+(20\d{2})', first_page)
    if published:
        words = re.findall(r'[A-Za-z0-9]+', clues[0][1])
        if len(words) >= 7:
            query = (f'site:proceedings.iclr.cc/paper_files/paper/{published[1]}/ '
                     f'"{" ".join(words[:3])}" "{" ".join(words[5:7])}"')
            entries.append(('pdf-publication', clues[0][1], query))
    entries.extend(('llm-primary' if i == 0 else 'llm-alternative', clues[0][1], query)
                   for i, query in enumerate(queries))
    entries.extend((origin, value, None) for origin, value in clues)
    attempts = []
    conflicts = []
    candidates = []
    seen = set()
    preprint = None
    acl_records = {}
    class CachedACL:
        def lookup(self, url):
            key = url.rstrip('/')
            if key not in acl_records:
                acl_records[key] = acl_lookup.lookup(url)
            return acl_records[key]
    cached_acl = CachedACL() if acl_lookup is not None else None
    for origin, query_title, query in entries:
        if cancelled():
            raise ValueError('书目核验已取消')
        if preprint is not None and origin in {'filename', 'catalog', 'pdf'}:
            continue
        if (origin == 'acl-id' and cached_acl is not None
                and acl_records.get(query.rstrip('/')) is not None
                and acl_records[query.rstrip('/')].status == 'verified'):
            # The structured record was already checked. A stripped Markdown
            # copy must not erase an actual DOI/title conflict from that page.
            continue
        normalized = _normalize(query or query_title)
        if origin == 'acl-metadata':
            normalized = 'acl-metadata:' + normalized
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        structured_acl = False
        if origin == 'acl-metadata':
            result = cached_acl.lookup(query)
            structured_acl = result.status == 'verified'
        elif origin == 'doi':
            result = official_lookup.lookup(query)
        elif origin in {'arxiv-id', 'acl-id'}:
            try:
                extracted = evidence.extract([query])
                bibliography = _extract_bibliography(query_title, query, extracted.payload) if extracted.status_code == 200 else {}
                # Do not let the parser's fallback title masquerade as an
                # independently observed title on the official page.
                title_pattern = (r'(?mi)^#\s*Title:\s*[^\n]+' if origin == 'arxiv-id'
                                 else r'(?m)^#{1,6}\s+[^\n]+')
                if not re.search(title_pattern, evidence.extracted):
                    bibliography = {}
                result = EnrichmentResult('verified' if bibliography else 'ambiguous',
                    1.0 if bibliography else None, bibliography,
                    {'source_url': query,
                     'source_domain': 'arxiv.org' if origin == 'arxiv-id' else 'aclanthology.org',
                     'reason': '' if bibliography else 'extraction_incomplete',
                     'message': '' if bibliography else '官方页面标题提取不完整，无法核验'})
            except (OSError, ValueError):
                result = EnrichmentResult('not_found', None, {}, {'message': '官方页面暂无法提取'})
        else:
            result = enrich_bibliography(query_title, year, venue, evidence, query=query, acl_lookup=cached_acl)
        structured_acl = (result.status == 'verified' and
                          result.enrichment.get('title_evidence') == 'citation_title')
        attempts.append({'origin': origin, 'query': query or query_title, 'status': result.status, 'message': result.enrichment.get('message', '')})
        candidates.extend(item for item in result.enrichment.get('candidates', []) if isinstance(item, dict))
        if result.status == 'unavailable':
            # Crossref is independent of Tavily. Its outage must not suppress
            # title-based lookup through the configured search gateway.
            if origin in {'doi', 'acl-metadata'}:
                continue
            if preprint is not None:
                break
            return EnrichmentResult(result.status, result.confidence, {}, {
                **result.enrichment, 'attempts': attempts, 'local_clues': clues, 'search_plan': plan})
        if result.status != 'verified':
            if result.status == 'conflict' and origin == 'doi':
                conflicts.append({'url': f'https://doi.org/{query}', 'title': query_title,
                                  'reason': 'identifier', 'identifiers': ['doi']})
            continue
        if result.enrichment.get('source_domain') not in _TRUSTED_DOMAINS:
            continue
        bibliography = result.bibliography
        canonical = str(bibliography.get('canonical_title') or '')
        # A search match alone must not authorize overwriting local metadata.
        # Require the extracted official title to agree with a local primary clue.
        matches = [source for source, value in clues
                   if _normalize(value) == _normalize(canonical) or
                   (source in {'markdown', 'pdf'} and
                    _corroborated_title(value, canonical, bibliography, front + '\n' + first_page))]
        primary_clues = {source for source, _ in clues if source in {'markdown', 'pdf'}}
        if not (primary_clues.intersection(matches) if primary_clues else 'filename' in matches):
            attempts[-1].update(status='conflict', message='官方标题与本地 PDF 或正文标题不一致')
            if origin in {'doi', 'arxiv-id', 'acl-id', 'acl-metadata'}:
                conflicts.append({'url': bibliography.get('official_url') or result.enrichment.get('source_url'),
                                  'title': canonical, 'reason': 'title'})
            continue
        if origin != 'doi' and not structured_acl and _normalize(canonical) not in _normalize(evidence.extracted):
            attempts[-1].update(status='ambiguous', message='官方页面未提取到完整标题')
            continue
        mismatches = [key for key, value in identifiers.items()
                      if bibliography.get(key) and str(bibliography[key]).casefold() != value.casefold()]
        if mismatches:
            attempts[-1].update(status='conflict', message='官方标识符与本地证据不一致')
            conflicts.append({'url': bibliography.get('official_url') or result.enrichment.get('source_url'),
                              'title': canonical, 'reason': 'identifier', 'identifiers': mismatches})
            continue
        disputed = {key for item in conflicts for key in item.get('identifiers', [])}
        if any(not bibliography.get(key) or str(bibliography[key]).casefold() != identifiers[key].casefold()
               for key in disputed):
            attempts[-1].update(status='ambiguous', message='已发现标识符冲突，当前记录缺少用于排除冲突的标识符')
            continue
        verified = EnrichmentResult('verified', result.confidence, bibliography, {
            **result.enrichment, 'attempts': attempts, 'local_clues': clues,
            'matched_clues': matches, 'identifier_clues': identifiers, 'search_plan': plan,
        })
        if bibliography.get('venue_type') == 'preprint' and queries:
            preprint = verified
            continue
        return verified
    if preprint is not None:
        return EnrichmentResult(preprint.status, preprint.confidence, preprint.bibliography, {
            **preprint.enrichment, 'publication_resolution': 'preprint_only',
            'message': '已匹配预印本，正式发表版本尚未确认'})
    if conflicts:
        return EnrichmentResult('conflict', None, {}, {
            'message': '官方记录与本地论文信息冲突，已保留原信息',
            'attempts': attempts, 'candidates': conflicts + candidates, 'local_clues': clues, 'search_plan': plan,
        })
    if attempts and all(item['status'] == 'not_found' for item in attempts):
        return EnrichmentResult('not_found', None, {}, {
            'message': '未找到可核实的官方论文记录',
            'attempts': attempts, 'candidates': candidates, 'local_clues': clues, 'search_plan': plan,
        })
    return EnrichmentResult('ambiguous', None, {}, {
        'message': '找到候选，但证据不足以确认是同一篇论文',
        'attempts': attempts, 'candidates': candidates, 'local_clues': clues, 'search_plan': plan,
    })


def repaired_short_name(short_name, canonical):
    # A word split out of a filename is not a separate acronym.
    if short_name and _normalize(canonical).startswith(_normalize(short_name) + ' '):
        return ''
    return short_name
