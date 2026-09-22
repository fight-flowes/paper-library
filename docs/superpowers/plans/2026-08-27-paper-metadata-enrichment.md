# Paper Metadata Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enrich each parsed paper with conservative, source-traceable bibliographic metadata through the local Tavily Gateway without making MinerU parsing dependent on the network.

**Architecture:** A focused `paper_metadata` module owns Gateway readiness, search/extract requests, candidate resolution, and enrichment states. The parser persists its result in schema-v2 metadata and a date-aware catalog; the reader consumes both schema versions and shows the resulting provenance.

**Tech Stack:** Python 3.11, httpx, Flask/Jinja, pytest/unittest, local Tavily Gateway at `127.0.0.1:18085`.

**Spec:** `docs/superpowers/specs/2026-08-27-paper-metadata-enrichment-design.md`

## Global Constraints

- Send only paper title, filename-derived year, and venue to the local Gateway; never send PDF or Markdown content.
- Use `TAVILY_GATEWAY_URL` with default `http://127.0.0.1:18085`, `trust_env=False`, a 35-second timeout, and `X-Project-ID: paper-library`.
- Gateway failures must publish usable MinerU output with an advisory enrichment status.
- Support existing schema-v1 metadata indefinitely; schema-v2 adds optional enrichment fields.
- Never overwrite `metadata.overrides.json` values during automatic enrichment or refresh.
- Tests must mock Gateway calls and never depend on a live key or network service.

---

### Task 1: Build the isolated enrichment client and resolver

**Files:**
- Create: `paper-library/scripts/paper_metadata.py`
- Create: `paper-library/tests/test_paper_metadata.py`
- Modify: `paper-library/requirements-reader.txt`

**Interfaces:**
- Produces `enrich_bibliography(title: str, year: str, venue: str, client: GatewayClient) -> EnrichmentResult`.
- Produces `GatewayClient(base_url: str, transport: httpx.BaseTransport | None = None)` with `ready()`, `search()`, and `extract()` methods.
- `EnrichmentResult.as_dict()` returns `bibliography` and `enrichment` JSON-safe mappings.

- [ ] **Step 1: Write failing resolver tests**

```python
def test_exact_official_result_is_verified() -> None:
    client = FakeGateway(search_results=[ACL_RESULT], extracted=[ACL_PAGE])
    result = enrich_bibliography("A Paper Title", "2025", "ACL", client)
    assert result.status == "verified"
    assert result.bibliography["publication_date"] == "2025-07-27"
    assert result.bibliography["acl_anthology_id"] == "2025.acl-long.1"

def test_ambiguous_results_preserve_no_external_fields() -> None:
    client = FakeGateway(search_results=[FIRST_MATCH, SECOND_MATCH])
    result = enrich_bibliography("A Paper Title", "", "", client)
    assert result.status == "ambiguous"
    assert result.bibliography == {}
```

- [ ] **Step 2: Run the resolver tests and verify they fail because the module is absent**

Run: `PYTHONPATH=. pytest tests/test_paper_metadata.py -v`

- [ ] **Step 3: Implement the minimal HTTP and matching layer**

```python
class GatewayClient:
    def __init__(self, base_url: str, transport: httpx.BaseTransport | None = None): ...
    def ready(self) -> GatewayResponse: ...
    def search(self, query: str) -> GatewayResponse: ...
    def extract(self, urls: list[str]) -> GatewayResponse: ...

def enrich_bibliography(title: str, year: str, venue: str, client: GatewayClient) -> EnrichmentResult: ...
```

Implement normalized title comparison, trusted-domain ranking, a confidence threshold plus runner-up margin, and `verified`, `ambiguous`, `not_found`, and `unavailable` states. Store request IDs, query, selected source URL, and failure message without raw search payloads.

- [ ] **Step 4: Add readiness and transient-failure tests, then make them pass**

```python
def test_unready_gateway_returns_unavailable_without_raising() -> None:
    client = FakeGateway(ready_status=503)
    assert enrich_bibliography("A Paper Title", "2025", "ACL", client).status == "unavailable"
```

Run: `PYTHONPATH=. pytest tests/test_paper_metadata.py -v`

- [ ] **Step 5: Add `httpx>=0.28,<1` and run focused tests**

Run: `PYTHONPATH=. pytest tests/test_paper_metadata.py -v`

### Task 2: Persist schema-v2 metadata and date-aware catalog records

**Files:**
- Modify: `paper-library/scripts/parse_pdf.py`
- Modify: `paper-library/tests/test_parse_pdf.py`

**Interfaces:**
- `build_metadata(..., enrichment: EnrichmentResult | None = None) -> dict[str, object]` writes schema version 2.
- `render_catalog(records)` emits existing columns plus `publication_date`.
- `_validate_metadata()` accepts v1 records and validates v2 optional `bibliography`/`enrichment` objects.

- [ ] **Step 1: Write failing parser tests**

```python
def test_build_metadata_records_verified_bibliography() -> None:
    metadata = parse_pdf.build_metadata(..., enrichment=VERIFIED_RESULT)
    assert metadata["schema_version"] == 2
    assert metadata["bibliography"]["doi"] == "10.1/example"

def test_catalog_uses_verified_publication_date() -> None:
    catalog = parse_pdf.render_catalog([VERIFIED_METADATA])
    assert catalog.splitlines()[1].endswith("\t2025-07-27")
```

- [ ] **Step 2: Run the focused parser tests and verify the expected failure**

Run: `PYTHONPATH=. pytest tests/test_parse_pdf.py -k 'bibliography or publication_date' -v`

- [ ] **Step 3: Implement schema compatibility and catalog projection**

Keep top-level filename fields as fallback. Project externally resolved title, year, venue, and date into the catalog only when enrichment status is `verified`. Leave `parsed_at` as a separate local processing timestamp.

- [ ] **Step 4: Wire best-effort enrichment into staged parsing**

Call the resolver after MinerU output is validated and before staged metadata is written. Catch only Gateway/resolution failures as `unavailable`; do not catch local metadata or publication errors. Apply `metadata.overrides.json` after enrichment and before catalog rendering.

- [ ] **Step 5: Run parser tests**

Run: `PYTHONPATH=. pytest tests/test_parse_pdf.py -v`

### Task 3: Add non-destructive command controls and metadata refresh

**Files:**
- Modify: `paper-library/scripts/parse_pdf.py`
- Modify: `paper-library/scripts/parse_pdf.sh`
- Modify: `paper-library/tests/test_parse_pdf.py`
- Modify: `paper-library/README.md`

**Interfaces:**
- `parse_pdf.py <pdf> --no-enrich` publishes parsed output with `status: skipped`.
- `parse_pdf.py <pdf> --refresh-metadata` reads existing package metadata, resolves only metadata, applies overrides, and atomically rewrites metadata/catalog without invoking MinerU.

- [ ] **Step 1: Write failing CLI tests**

```python
def test_no_enrich_does_not_construct_gateway_client(self) -> None: ...
def test_refresh_metadata_does_not_run_mineru(self) -> None: ...
def test_manual_override_wins_over_verified_value(self) -> None: ...
```

- [ ] **Step 2: Run these tests and verify they fail**

Run: `PYTHONPATH=. pytest tests/test_parse_pdf.py -k 'no_enrich or refresh_metadata or override' -v`

- [ ] **Step 3: Implement parser flags and atomic refresh**

Use a mutually exclusive CLI mode for `--refresh-metadata` and `--force`. Preserve the existing package if refresh fails before the final atomic replace. Do not require MinerU for refresh.

- [ ] **Step 4: Document commands**

Add examples for ordinary automatic enrichment, offline parsing with `--no-enrich`, and later refresh. Document that title metadata is sent to the locally hosted Gateway but PDF contents are not.

- [ ] **Step 5: Run parser and shell-wrapper tests**

Run: `PYTHONPATH=. pytest tests/test_parse_pdf.py -v`

### Task 4: Consume formal publication metadata in the reader

**Files:**
- Modify: `paper-library/reader/library.py`
- Modify: `paper-library/reader/templates/library.html`
- Modify: `paper-library/reader/templates/paper.html`
- Modify: `paper-library/reader/static/reader.css`
- Modify: `paper-library/tests/test_reader_library.py`
- Modify: `paper-library/tests/test_reader_app.py`

**Interfaces:**
- `PaperRecord` includes `publication_date: str`.
- `LibrarySnapshot.grouped()` sorts by publication date, then year, then parsed date.
- Detail template receives `metadata.bibliography` and `metadata.enrichment` without requiring either for legacy records.

- [ ] **Step 1: Write failing reader tests**

```python
def test_library_sorts_verified_publication_dates_before_parse_dates(...) -> None: ...
def test_paper_page_shows_verified_doi_and_official_link(client, project_root) -> None: ...
def test_paper_page_labels_ambiguous_metadata_as_advisory(client, project_root) -> None: ...
```

- [ ] **Step 2: Run the focused reader tests and verify they fail**

Run: `PYTHONPATH=. pytest tests/test_reader_library.py tests/test_reader_app.py -k 'publication or verified or ambiguous' -v`

- [ ] **Step 3: Implement catalog compatibility, sorting, and templates**

Accept old ten-column catalogs and new date-aware catalogs. Render formal date on list rows when present. Render authors, venue/type, DOI and official links only when provided. Escape all values through Jinja and use `rel="noopener"` on external links.

- [ ] **Step 4: Add compact styles and verify focused tests**

Run: `PYTHONPATH=. pytest tests/test_reader_library.py tests/test_reader_app.py -v`

### Task 5: Full regression verification and operator handoff

**Files:**
- Modify: `paper-library/README.md`
- Test: `paper-library/tests/`

- [ ] **Step 1: Run the complete test suite**

Run: `PYTHONPATH=. pytest tests`
Expected: all existing and new tests pass without network access.

- [ ] **Step 2: Exercise offline fallback**

Run the parser test fixture with a mocked unavailable Gateway and verify it publishes searchable Markdown, schema-v2 metadata with `status: unavailable`, and a catalog row.

- [ ] **Step 3: Check documentation and diff hygiene**

Run: `git diff --check`

- [ ] **Step 4: Commit the implementation**

```bash
git add paper-library
git commit -m "feat: enrich paper metadata through local gateway"
```
