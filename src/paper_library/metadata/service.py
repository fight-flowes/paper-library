"""Best-effort bibliographic enrichment through the local Tavily Gateway."""

from __future__ import annotations

import os
import html
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Protocol
from urllib.parse import urljoin, urlparse

import httpx


DEFAULT_GATEWAY_URL = "http://127.0.0.1:18085"
PROJECT_ID = "paper-library"
_TRUSTED_DOMAINS = frozenset(
    {
        "aclanthology.org",
        "openreview.net",
        "arxiv.org",
        "api.crossref.org",
        "doi.org",
        "nature.com",
        "proceedings.iclr.cc",
    }
)
_MONTHS = {
    "january": "01",
    "february": "02",
    "march": "03",
    "april": "04",
    "may": "05",
    "june": "06",
    "july": "07",
    "august": "08",
    "september": "09",
    "october": "10",
    "november": "11",
    "december": "12",
}


@dataclass(frozen=True)
class GatewayResponse:
    status_code: int
    payload: Mapping[str, object]
    request_id: str | None = None
    message: str = ""


@dataclass(frozen=True)
class EnrichmentResult:
    status: str
    confidence: float | None
    bibliography: dict[str, object]
    enrichment: dict[str, object]


class Gateway(Protocol):
    def ready(self) -> GatewayResponse: ...

    def search(self, query: str) -> GatewayResponse: ...

    def extract(self, urls: list[str]) -> GatewayResponse: ...


class GatewayClient:
    """A small client for the loopback-only Tavily Gateway."""

    def __init__(
        self,
        base_url: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or os.getenv("TAVILY_GATEWAY_URL") or DEFAULT_GATEWAY_URL).rstrip("/")
        self.transport = transport

    def ready(self) -> GatewayResponse:
        return self._request("GET", "/health/ready")

    def search(self, query: str) -> GatewayResponse:
        return self._request(
            "POST",
            "/search",
            json={
                "query": query,
                "search_depth": "basic",
                "max_results": 5,
            },
        )

    def extract(self, urls: list[str]) -> GatewayResponse:
        return self._request(
            "POST",
            "/extract",
            json={"urls": urls, "extract_depth": "basic", "format": "markdown"},
        )

    def _request(
        self,
        method: str,
        path: str,
        json: dict[str, object] | None = None,
    ) -> GatewayResponse:
        last_response: GatewayResponse | None = None
        for attempt in range(3):
            try:
                with httpx.Client(
                    base_url=self.base_url,
                    timeout=35,
                    trust_env=False,
                    transport=self.transport,
                    headers={"X-Project-ID": PROJECT_ID},
                ) as client:
                    response = client.request(method, path, json=json)
            except httpx.RequestError as error:
                last_response = GatewayResponse(503, {}, message=str(error))
            else:
                try:
                    payload = response.json()
                except ValueError:
                    payload = {}
                if not isinstance(payload, Mapping):
                    payload = {}
                last_response = GatewayResponse(
                    response.status_code,
                    payload,
                    response.headers.get("X-Gateway-Request-ID"),
                    "" if response.is_success else _gateway_error_message(payload),
                )
                if response.status_code not in {502, 503}:
                    return last_response
            if attempt < 2:
                time.sleep(0.1 * (attempt + 1))
        assert last_response is not None
        return last_response


def enrich_bibliography(
    title: str,
    year: str,
    venue: str,
    gateway: Gateway,
    *, query: str | None = None, acl_lookup=None,
) -> EnrichmentResult:
    """Resolve one paper title without allowing lookup failures to escape."""

    searched_at = datetime.now(timezone.utc).isoformat()
    request_ids: list[str] = []
    base = {
        "searched_at": searched_at,
        "query": query or title,
        "source_url": None,
        "source_domain": None,
        "gateway_request_ids": request_ids,
        "message": "",
    }

    try:
        ready = gateway.ready()
    except Exception as error:
        return _unavailable(base, str(error))
    _append_request_id(request_ids, ready)
    if ready.status_code != 200:
        return _unavailable(base, ready.message or "本机检索服务未就绪")

    try:
        searched = gateway.search(query if query is not None else f'"{title}"')
    except Exception as error:
        return _unavailable(base, str(error))
    _append_request_id(request_ids, searched)
    if searched.status_code != 200:
        return _unavailable(base, searched.message or "标题检索失败")

    search_payloads = [searched.payload]
    candidates = _rank_candidates(title, year, venue, _merge_search_payloads(search_payloads))
    if "acl" in venue.casefold() and not any(
        candidate.domain == "aclanthology.org" for candidate in candidates
    ):
        try:
            acl_search = gateway.search(f'site:aclanthology.org "{title}"')
        except Exception:
            acl_search = None
        if acl_search is not None:
            _append_request_id(request_ids, acl_search)
            if acl_search.status_code == 200:
                search_payloads.append(acl_search.payload)
                candidates = _rank_candidates(
                    title,
                    year,
                    venue,
                    _merge_search_payloads(search_payloads),
                )
    if not candidates:
        return EnrichmentResult("not_found", None, {}, {**base, "message": "未找到匹配论文", "candidates": []})
    base['candidates'] = _candidate_summaries(candidates)
    winner, *remaining = candidates
    # A shortened search title may be enough to inspect one trusted source;
    # the extracted official title is still checked against the local paper.
    inspectable = winner.domain in _TRUSTED_DOMAINS and winner.score >= 0.78
    if (winner.score < 0.9 and not inspectable) or (remaining and winner.score - remaining[0].score < 0.08):
        return EnrichmentResult("ambiguous", None, {}, {**base, "message": "检索结果无法唯一确认",
                                                         "candidates": _candidate_summaries(candidates)})

    if winner.domain == 'aclanthology.org' and acl_lookup is not None:
        official = acl_lookup.lookup(winner.url)
        if official.status == 'verified':
            return EnrichmentResult(official.status, official.confidence, official.bibliography,
                                    {**base, **official.enrichment})

    try:
        extracted = gateway.extract([winner.url])
    except Exception as error:
        return _unavailable(base, str(error))
    _append_request_id(request_ids, extracted)
    if extracted.status_code != 200:
        return _unavailable(base, extracted.message or "官方页面提取失败")

    bibliography = _extract_bibliography(title, winner.url, extracted.payload)
    if not bibliography:
        return EnrichmentResult("ambiguous", None, {}, {**base, "message": "官方页面未提供可验证的书目信息"})
    base.update(
        {
            "source_url": winner.url,
            "source_domain": winner.domain,
            "message": "",
        }
    )
    return EnrichmentResult("verified", round(min(winner.score, 1.0), 2), bibliography, base)


def _candidate_summaries(candidates: list[_Candidate]) -> list[dict[str, object]]:
    return [dict(url=item.url, title=item.title, domain=item.domain, score=round(item.score, 2))
            for item in candidates[:5]
            if item.domain in _TRUSTED_DOMAINS and urlparse(item.url).scheme == 'https']


@dataclass(frozen=True)
class _Candidate:
    url: str
    domain: str
    score: float
    title: str = ''


def _rank_candidates(
    title: str,
    year: str,
    venue: str,
    payload: Mapping[str, object],
) -> list[_Candidate]:
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        return []
    normalized_title = _normalize(title)
    candidates_by_work: dict[str, _Candidate] = {}
    for item in raw_results:
        if not isinstance(item, Mapping):
            continue
        candidate_title = item.get("title")
        url = item.get("url")
        content = item.get("content")
        if not isinstance(candidate_title, str) or not isinstance(url, str):
            continue
        candidate_normalized = _normalize(candidate_title)
        parsed = urlparse(url)
        domain = (parsed.hostname or '').casefold().removeprefix('www.')
        arxiv_id = _arxiv_id(url)
        if arxiv_id:
            url = f'https://arxiv.org/abs/{arxiv_id}'
            # Search display titles are often shortened; only use the explicit
            # arXiv title field, never an arbitrary mention in the snippet.
            explicit_title = re.search(r'(?mi)^#\s*Title:\s*([^\n]+)', content or '')
            if explicit_title and re.search(r'\.\.\.|…', candidate_title):
                candidate_normalized = _normalize(explicit_title[1].split('[...]', 1)[0])
        acl_id = _acl_id(url)
        if acl_id and urlparse(url).path.endswith('.pdf'):
            url = f'https://aclanthology.org/{acl_id}/'
        display_title = re.sub(r'^\[\d{4}\.\d{4,5}\]\s*', '', candidate_title)
        display_prefix = _normalize(re.split(r'\.\.\.|…', display_title, maxsplit=1)[0])
        truncated = bool(re.search(r'\.\.\.|…', candidate_title))
        if (
            candidate_normalized == normalized_title
            or candidate_normalized.endswith(f" {normalized_title}")
            or candidate_normalized.startswith(f"{normalized_title} ")
        ):
            score = 0.8
        elif truncated and len(display_prefix.split()) >= 5 and normalized_title.startswith(display_prefix):
            score = 0.72
        elif _token_overlap(normalized_title, candidate_normalized) >= 0.95:
            score = 0.72
        elif (
            domain == "aclanthology.org"
            and _token_overlap(normalized_title, candidate_normalized) >= 0.75
        ):
            score = 0.66
        else:
            continue
        score += _source_bonus(domain)
        evidence = " ".join(
            value
            for value in (candidate_title, content, url)
            if isinstance(value, str)
        ).casefold()
        if year and year in evidence:
            score += 0.03
        if venue and venue.casefold() in evidence:
            score += 0.02
        candidate = _Candidate(url, domain, score, candidate_title)
        work_key = _candidate_work_key(candidate)
        existing = candidates_by_work.get(work_key)
        if existing is None or _candidate_sort_key(candidate) < _candidate_sort_key(existing):
            candidates_by_work[work_key] = candidate
    return sorted(candidates_by_work.values(), key=_candidate_sort_key)


def _extract_bibliography(
    fallback_title: str,
    url: str,
    payload: Mapping[str, object],
) -> dict[str, object]:
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        return {}
    raw_content = next(
        (
            item.get("raw_content")
            for item in raw_results
            if isinstance(item, Mapping) and item.get("url") == url
        ),
        None,
    )
    if not isinstance(raw_content, str):
        return {}
    domain = urlparse(url).netloc.casefold().removeprefix("www.")
    if domain == "aclanthology.org":
        return _extract_acl_bibliography(fallback_title, url, raw_content)
    if domain == "arxiv.org":
        return _extract_arxiv_bibliography(fallback_title, url, raw_content)
    if domain == "proceedings.iclr.cc":
        return _extract_iclr_bibliography(url, raw_content)
    observed_title = _observed_title(raw_content, fallback_title)
    if not observed_title:
        return {}
    bibliography: dict[str, object] = {
        "canonical_title": observed_title,
        "official_url": url,
    }
    if domain == 'nature.com':
        journal = re.search(r'\[\*([^*\n]+)\*\]\([^)]*\)', raw_content[:3000])
        if journal:
            bibliography.update(venue=journal[1], venue_type='journal')
        elif '/articles/s41586-' in url:
            bibliography.update(venue='Nature', venue_type='journal')
        published = re.search(r'(?im)^Published(?: online)?:\s*(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})', raw_content)
        if published and published[2].lower() in _MONTHS:
            bibliography['publication_date'] = f'{published[3]}-{_MONTHS[published[2].lower()]}-{int(published[1]):02d}'
    authors = _field(raw_content, "Authors?")
    if authors:
        bibliography["authors"] = [part.strip() for part in authors.split(",") if part.strip()]
    venue = _field(raw_content, "Venue")
    if venue:
        bibliography["venue"] = venue
        bibliography["venue_type"] = "conference" if "acl" in venue.casefold() else "other"
    date = _field(raw_content, "Publication date|Date")
    if date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        bibliography["publication_date"] = date
    doi = _field(raw_content, "DOI")
    if doi:
        bibliography["doi"] = normalize_doi(doi)
    return bibliography


def _extract_arxiv_bibliography(
    fallback_title: str,
    url: str,
    content: str,
) -> dict[str, object]:
    arxiv_id = _arxiv_id(url)
    if not arxiv_id:
        return {}
    title_match = re.search(r"(?mi)^#[ \t]*Title:[ \t]*([^\n]+?)[ \t]*$", content)
    if not title_match:
        return {}
    canonical_title = title_match.group(1).strip()
    year_prefix = int(arxiv_id[:2])
    full_year = 2000 + year_prefix if year_prefix <= 90 else 1900 + year_prefix
    publication_date = f"{full_year:04d}-{arxiv_id[2:4]}"
    official_url = f"https://arxiv.org/abs/{arxiv_id}"
    bibliography = {
        "canonical_title": canonical_title,
        "venue": "arXiv",
        "venue_type": "preprint",
        "publication_date": publication_date,
        "doi": f"10.48550/arXiv.{arxiv_id}",
        "arxiv_id": arxiv_id,
        "official_url": official_url,
        "official_pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
    }
    journal_ref = re.search(r'(?mi)^\|\s*Journal\s+reference:\s*\|\s*([^|\n]+)', content)
    if journal_ref:
        reference = journal_ref[1].strip()
        venue = re.split(r',\s*pp\.\s*\d', reference, maxsplit=1, flags=re.I)[0].strip(' ,')
        bibliography['venue'] = venue
        bibliography['venue_type'] = ('conference' if re.search(r'(?i)\b(?:proceedings|conference|symposium|workshop)\b', venue)
                                      else 'journal')
        year = re.search(r'\b(?:19|20)\d{2}\b', reference)
        if year:
            bibliography['publication_date'] = year[0]
        bibliography.pop('doi')
        related = re.search(r'(?mi)^\|\s*Related DOI:\s*\|\s*([^|\n]+)', content)
        doi = re.search(r'https?://doi\.org/(10\.\d{4,9}/[^\s<>]+)', related[1]) if related else None
        if doi:
            bibliography['doi'] = doi[1].rstrip('.,;')
    return bibliography


def _extract_iclr_bibliography(url: str, content: str) -> dict[str, object]:
    match = re.fullmatch(
        r'/paper_files/paper/(20\d{2})/hash/[0-9a-f]+-Abstract-Conference\.html',
        urlparse(url).path,
    )
    if not match:
        return {}
    year = match[1]
    venue = re.search(
        rf'International Conference on Learning Representations\s+{year}\s+\(ICLR\s+{year}\)',
        content, re.I,
    )
    heading = re.search(r'(?m)^#\s+(.+?)\s*$', content)
    if not heading or not venue:
        return {}
    bibliography: dict[str, object] = {
        'canonical_title': heading[1].strip(),
        'venue': f'International Conference on Learning Representations {year} (ICLR {year})',
        'venue_type': 'conference',
        'publication_date': year,
        'official_url': url,
    }
    author_line = next((line.strip() for line in content[heading.end():].splitlines() if line.strip()), '')
    if author_line and not author_line.startswith('['):
        authors = [name.strip() for name in author_line.split(',') if name.strip()]
        if len(authors) >= 2:
            bibliography['authors'] = authors
    paper_link = re.search(r'\[Paper\]\((/paper_files/paper/[^)]+\.pdf)\)', content)
    if paper_link:
        bibliography['official_pdf_url'] = urljoin(url, paper_link[1])
    return bibliography


def _extract_acl_bibliography(
    fallback_title: str,
    url: str,
    content: str,
) -> dict[str, object]:
    title_heading = _acl_title_heading(content, url)
    if title_heading is None:
        return {}
    title, heading_end = title_heading
    bibliography: dict[str, object] = {
        "canonical_title": title,
        "venue_type": "conference",
        "official_url": url,
    }
    author_region = re.split(r'(?m)^#{1,6}\s+', content[heading_end:], maxsplit=1)[0]
    authors = list(dict.fromkeys(re.findall(
        r'\[([^\]]+)\]\((?:https://(?:www\.)?aclanthology\.org)?/people/[^)]*\)', author_region)))
    if not authors:
        simple_authors = _field(content, "Authors?")
        authors = (
            [part.strip() for part in simple_authors.split(",") if part.strip()]
            if simple_authors
            else []
        )
    if authors:
        bibliography["authors"] = authors
    identifier = _definition_field(content, "Anthology ID") or urlparse(url).path.strip("/").removesuffix('.pdf')
    if identifier:
        bibliography["acl_anthology_id"] = identifier
    venue = (
        _definition_field(content, "Volume")
        or _definition_field(content, "Venue")
        or _field(content, "Venue")
    )
    if venue:
        bibliography["venue"] = venue
    year = _definition_field(content, "Year")
    month = _definition_field(content, "Month")
    if year and re.fullmatch(r"\d{4}", year):
        month_number = _MONTHS.get((month or "").casefold())
        bibliography["publication_date"] = (
            f"{year}-{month_number}" if month_number else year
        )
    else:
        simple_date = _field(content, "Publication date|Date")
        if simple_date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", simple_date):
            bibliography["publication_date"] = simple_date
    doi = _definition_field(content, "DOI") or _field(content, "DOI")
    if doi:
        bibliography["doi"] = normalize_doi(doi)
    pdf_url = _definition_field(content, "PDF")
    if pdf_url and pdf_url.startswith("https://"):
        bibliography["official_pdf_url"] = pdf_url
    return bibliography


def _acl_title_heading(content: str, url: str) -> tuple[str, int] | None:
    """Prefer the heading linked to this paper; never guess from navigation."""
    matched, plain = {}, {}
    navigation = {'details', 'abstract', 'correct metadata for', 'export citation',
                  'markdown informal', 'markdown', 'bibtex', 'acl', 'cite',
                  'references', 'about', 'navigation', 'menu', 'home'}
    for match in re.finditer(r'(?m)^(#{1,6})\s+(.+?)\s*$', content):
        raw = match[2].strip()
        link = re.fullmatch(r'\[([^\]]+)\]\(([^)]+)\)', raw)
        title = link[1].strip() if link else raw
        if not title or _normalize(title) in navigation:
            continue
        if link:
            target = urljoin(url, link[2])
            identifier = _acl_id(target)
            if identifier:
                if identifier == _acl_id(url):
                    matched[_normalize(title)] = (title, match.end())
                continue
            # Keep support for stripped, relative PDF links in old extracts.
            if urlparse(target).hostname != urlparse(url).hostname or not urlparse(target).path.endswith('.pdf'):
                continue
        if len(match[1]) <= 2:
            plain[_normalize(title)] = (title, match.end())
    candidates = matched or plain
    return next(iter(candidates.values())) if len(candidates) == 1 else None


def _field(content: str, name: str) -> str | None:
    match = re.search(rf"(?mi)^(?:{name}):[ \t]*([^\n]+?)[ \t]*$", content)
    return match.group(1).strip() if match else None


def _observed_title(content: str, query_title: str) -> str | None:
    """Select an observed article heading, excluding publisher navigation."""
    ignored = {'nature', 'openreview', 'article', 'research article', 'menu', 'home',
               'navigation', 'search', 'about', 'access options', 'sign in', 'login',
               'skip to main content', 'details', 'download pdf'}
    candidates = []
    for match in re.finditer(r'(?m)^(#{1,2})[ \t]+([^\n]+)', content):
        raw = match[2].strip()
        link = re.fullmatch(r'\[([^\]]+)\]\([^)]*\)', raw)
        title = link[1] if link else raw
        normalized = _normalize(title)
        if normalized in {'abstract', 'references', 'bibliography'}:
            break
        if normalized and normalized not in ignored:
            candidates.append(title)
    matching = list(dict.fromkeys(t for t in candidates if _normalize(t) == _normalize(query_title)))
    if len(matching) == 1:
        return matching[0]
    unique = list(dict.fromkeys(candidates))
    return unique[0] if len(unique) == 1 else None


def _first_heading(content: str) -> str | None:
    match = re.search(
        r"(?m)^#{1,6}\s+(?:\[([^\]]+)\]\([^)]*\)|(.+?))\s*$",
        content,
    )
    if not match:
        return None
    return (match.group(1) or match.group(2)).strip()


def _definition_field(content: str, name: str) -> str | None:
    match = re.search(rf"(?mi)^{re.escape(name)}:\s*\n:\s*(.+?)\s*$", content)
    if not match:
        return None
    value = match.group(1).strip()
    link = re.fullmatch(r"\[([^\]]+)\]\([^)]*\)", value)
    if link:
        return link.group(1).strip()
    return value.removeprefix("<").removesuffix(">")


def _source_bonus(domain: str) -> float:
    if domain == "nature.com":
        return 0.2
    if domain == "aclanthology.org":
        return 0.24
    if domain == "openreview.net":
        return 0.18
    if domain == "proceedings.iclr.cc":
        return 0.26
    if domain.endswith(".acm.org") or domain.endswith(".ieee.org"):
        return 0.16
    if domain == "arxiv.org":
        return 0.1
    if domain == "doi.org":
        return 0.08
    return 0.0


def _merge_search_payloads(
    payloads: list[Mapping[str, object]],
) -> dict[str, object]:
    results: list[object] = []
    for payload in payloads:
        raw_results = payload.get("results")
        if isinstance(raw_results, list):
            results.extend(raw_results)
    return {"results": results}


def _arxiv_id(url: str) -> str | None:
    if urlparse(url).netloc.casefold().removeprefix("www.") != "arxiv.org":
        return None
    match = re.fullmatch(r"/(?:abs|html|pdf)/(\d{4}\.\d{4,5})(?:v\d+)?(?:\.pdf)?/?", urlparse(url).path)
    return match.group(1) if match else None


def _acl_id(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.netloc.casefold().removeprefix('www.') != 'aclanthology.org':
        return None
    path = parsed.path.strip('/')
    # Modern mirror PDFs and historical directory layouts share the paper ID
    # in their filename. Do not turn arbitrary ACL site paths into paper IDs.
    if '/' in path and not path.endswith('.pdf'):
        return None
    identifier = path.rsplit('/', 1)[-1].removesuffix('.pdf')
    if re.fullmatch(r'(?:\d{4}\.[a-z][a-z0-9-]*\.\d+|[A-Z]\d{2}-\d{4})', identifier):
        return identifier
    return None


def _candidate_work_key(candidate: _Candidate) -> str:
    arxiv_id = _arxiv_id(candidate.url)
    if arxiv_id:
        return f"arxiv:{arxiv_id}"
    acl_id = _acl_id(candidate.url)
    if acl_id:
        return f"acl:{acl_id}"
    return candidate.url.rstrip("/")


def _candidate_sort_key(candidate: _Candidate) -> tuple[float, int, str]:
    path = urlparse(candidate.url).path
    canonical_priority = 1
    if candidate.domain == "arxiv.org" and path.startswith("/abs/"):
        canonical_priority = 0
    elif candidate.domain == "aclanthology.org" and not path.endswith(".pdf"):
        canonical_priority = 0
    return (-candidate.score, canonical_priority, candidate.url)


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.findall(r"[\w]+", normalized))


def normalize_doi(value: str) -> str:
    value = html.unescape(value).strip().strip('<>')
    return re.sub(r'^(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)', '', value,
                  flags=re.I).rstrip('.,;')


def _token_overlap(left: str, right: str) -> float:
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens)


def _append_request_id(request_ids: list[str], response: GatewayResponse) -> None:
    if response.request_id and response.request_id not in request_ids:
        request_ids.append(response.request_id)


def _unavailable(base: dict[str, object], message: str) -> EnrichmentResult:
    return EnrichmentResult("unavailable", None, {}, {**base, "message": message})


def _gateway_error_message(payload: Mapping[str, object]) -> str:
    detail = payload.get("detail")
    if isinstance(detail, Mapping) and isinstance(detail.get("error"), str):
        return detail["error"]
    return "本机检索服务请求失败"
