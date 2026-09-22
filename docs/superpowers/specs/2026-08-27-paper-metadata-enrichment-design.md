# Paper metadata enrichment design

## Goal

Extend the local paper library so a successful MinerU conversion also attempts
to resolve trustworthy bibliographic metadata from the paper title. The reader
then shows formal publication information alongside the local PDF and Markdown.

The system must remain useful offline: MinerU conversion and publication never
fail merely because web metadata is unavailable or ambiguous.

## Scope

First release resolves and displays:

- canonical title and author list;
- venue, venue type (`conference`, `journal`, `preprint`, or `other`), and
  publication date;
- DOI, arXiv ID, ACL Anthology ID, OpenReview ID when evidenced;
- official landing page and official PDF URL;
- provenance, match confidence, and enrichment state.

It does not build a citation graph, infer citation counts, scrape Google
Scholar, or automatically generate a related-paper network. Those are a
separate future feature after the bibliographic records are reliable.

## Architecture

```text
PDF -> MinerU -> staged Markdown -> base metadata
                              -> title query via local Tavily Gateway
                              -> candidate scoring and official-page extraction
                              -> enriched metadata + catalog -> reader
```

The enrichment module is a new library module used by `scripts/parse_pdf.py`.
It owns Gateway HTTP calls, candidate selection, field extraction, and the
provenance record. MinerU remains responsible only for PDF-to-Markdown.

The module sends only the title and, when available, filename-derived year and
venue to the Gateway. It never uploads local PDF or Markdown content.

## Gateway contract

- Base URL: `TAVILY_GATEWAY_URL`, default `http://127.0.0.1:18085`.
- Use `httpx.Client(timeout=35, trust_env=False)` and
  `X-Project-ID: paper-library`.
- Check `/health/ready` once before an enrichment attempt.
- Search exact-title queries with at most five results, then extract at most
  two high-quality candidate pages.
- Prefer results from ACL Anthology, OpenReview, arXiv, DOI registries, and
  official publisher or conference domains.
- Record Gateway request IDs and response status, never an API key, admin token,
  or full raw response.
- Treat 400 as non-retryable. Retry transient 502/503 failures at most twice
  with jitter; record the final failure state.

## Resolution policy

Candidate selection is deterministic and conservative:

1. Normalize whitespace, punctuation, Unicode, and case in titles.
2. Require an exact normalized title match or a high token-overlap score.
3. Add corroborating score for agreement with known filename year/venue and
   for an official domain.
4. Auto-select only a single candidate above the confidence threshold and with
   a margin over the runner-up. Otherwise retain base metadata and mark the
   result `ambiguous`.

The selected source becomes field-level evidence. Data found only in search
snippets is marked lower confidence than data extracted from the official page.

## Metadata and catalog

New files use `metadata.json` schema version 2. The reader and parser continue
to read schema version 1 so existing papers remain usable.

Schema version 2 preserves the current top-level local fields and adds:

```json
{
  "bibliography": {
    "canonical_title": "...",
    "authors": ["..."],
    "venue": "...",
    "venue_type": "conference",
    "publication_date": "2025-07-27",
    "doi": "...",
    "arxiv_id": "...",
    "acl_anthology_id": "...",
    "openreview_id": "...",
    "official_url": "https://...",
    "official_pdf_url": "https://..."
  },
  "enrichment": {
    "status": "verified",
    "confidence": 0.97,
    "searched_at": "...",
    "query": "...",
    "source_url": "https://...",
    "source_domain": "...",
    "gateway_request_ids": ["..."],
    "message": "..."
  }
}
```

`status` is one of `verified`, `ambiguous`, `not_found`, `unavailable`, or
`skipped`. A missing date/identifier remains `null`; it is never invented.

The catalog gains a `publication_date` index column. It receives externally
resolved title, year, venue, and date only for a `verified` match; otherwise it
uses the current filename-derived identity. Lists sort by formal publication
date, then year, then local parse date.

Manual corrections live beside each package in `metadata.overrides.json`.
They have higher precedence than enrichment and are never overwritten by a
refresh. Their use is recorded in the rendered metadata as `manual` source.

## CLI and execution flow

`parse_pdf.sh <pdf>` remains the normal command and enables best-effort
enrichment by default after MinerU has produced validated staged Markdown.

- `--no-enrich` skips all Gateway calls and writes `status: skipped`.
- `--refresh-metadata <pdf>` refreshes metadata and the catalog without running
  MinerU again. It supports existing papers and is the recovery path after a
  temporary Gateway outage.
- `--force` retains its current meaning for changed PDFs; it does not override
  a manual metadata override.

Before publication, the staged metadata and staged catalog are validated in the
existing transaction. A Gateway failure produces a valid base metadata record;
only malformed local metadata or publication failures abort the parse.

## Reader behavior

The paper detail page adds a compact bibliographic section below the title:
authors, full publication date, venue/type, identifiers, official page, and
the source status. `verified` is visually neutral; `ambiguous`, `not_found`,
and `unavailable` are clearly advisory rather than errors. The source PDF and
local Markdown remain the primary reading paths.

The library list displays formal publication date when available and continues
to work for legacy metadata.

## Testing and acceptance

Tests use mocked local HTTP responses; no test contacts the Gateway or needs a
Tavily key. They cover:

- verified official match and schema-v2 publication;
- ambiguous and no-result records preserving filename metadata;
- readiness and transient Gateway failures degrading without aborting MinerU;
- manual overrides taking precedence;
- schema-v1 compatibility and catalog/date ordering;
- reader rendering of verified and advisory metadata states.

Acceptance requires a successful MinerU parse with the Gateway unavailable,
a verified fixture with complete provenance, and the existing reader/parse test
suite passing.
