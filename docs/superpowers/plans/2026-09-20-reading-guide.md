# Adaptive reading guide implementation plan

Goal: generate Chinese reading guidance from actual Markdown sections with verifiable source locations, using the existing .env model configuration.

Architecture: reading/structure.py extracts ordered sections and source blocks without fixed section-name categories. reading/service.py performs bounded concurrent chunk reading, validates quotes and references, synthesizes a whole-paper guide and atomically persists results. scripts/guide runs generation offline. Reader renders saved guidance without model requests, and marks outdated source versions.

User has authorized implementation directly and requested no repeated approval. Execute locally without unrelated refactoring or commits.

- [x] Extract actual headings, infer numeric sublevels without renaming headings, preserve unnumbered titles; add stable paragraph anchors to reader without changing translation identifiers.
- [x] Test unusual headings, no headings, long sections, duplicate headings and fixed token limits. Implement chunking with complete source coverage.
- [x] Implement model generation with current configured model/concurrency, validated verbatim quotations, typed claims, reusable per-chunk cache, retry of invalid output and atomic publish. A failed section must not publish an apparently complete guide. Check source unchanged before publication.
- [x] Add guide run/status CLI with library/paper/all scope and force option; no arguments prints help. Test unknown papers, stale results and model errors.
- [x] Add collapsed whole-paper and per-section guidance with source links and safe escaped text. Hide stale guidance content while showing regeneration notice; no model requests from GET.
- [x] Run unit/integration tests and browser navigation checks. Generate ws-p0002 guide with configured Qwen and inspect actual evidence and rendered result. Document commands and known text-only limitations.

Review focus: text inside fenced code is not a section; captions/table cells are not promoted to sections; oversized blocks remain fully covered; evidence must refer to supplied text with a matching quotation; stale artifacts cannot imply they describe current text; partial failures retain the previous valid guide; model strings never become trusted HTML.

Validation: 161 tests passed; browser translation, management and guide flows passed. Qwen generated ws-p0002 successfully (30 section regions, 24 with substantive guidance); all displayed evidence links resolve. Existing 1226 cached translation segments remain available.
