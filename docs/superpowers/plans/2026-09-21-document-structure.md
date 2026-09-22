# Document structure annotation implementation

Approved scope: annotate existing text without rewriting, merging, deleting, or reordering it. Preserve translation IDs, source anchors, guides, math, poetry whitespace, HTML tables, code and lists. Model configuration comes from the local .env. No visual/PDF interpretation is claimed when only Markdown exists.

1. Add independent Markdown block extraction, strict role validation, source/version-bound sidecar persistence, and tests for multilingual and special text.
2. Add bounded model batches with neighboring context, configured temperature/concurrency, per-batch resumable cache, cancellation and atomic publication. Unknown annotations retain existing rendering; stale/corrupt sidecars are ignored.
3. Apply annotations after translation slot generation to avoid changes to cache/anchors. Existing heading levels can change visually; annotated paragraph headings retain their source anchors. Protected blocks remain untouched.
4. Add scripts/structure run/status and integrate as an import stage before translation/guide, with progress and nonfatal warning outcomes.
5. Test malformed output, missing/duplicate IDs, cancellation, source changes, reuse, special-text preservation and workflow failure fallback. Run existing suite and browser checks. Generate and inspect ws-p0003, ws-p0001 and a formula-heavy paper.

First phase does not alter guide segmentation or regenerate translations. Model role judgements remain fallible; originals are the fallback. No hidden text deletion, spelling repair or prose rewriting.
