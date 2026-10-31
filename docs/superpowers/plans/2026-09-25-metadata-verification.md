# Metadata verification improvements

Goal: increase first-pass bibliographic verification without publishing a different paper's metadata, and make unresolved outcomes actionable.

1. Identifier lookup: extract trusted DOI/arXiv/ACL leads from PDF front matter, filename, and existing metadata; query official records directly; accept only when the official title matches independently parsed paper evidence. Add unit tests for correct, conflicting, and unavailable lookups.
2. Title and candidate handling: remove known front-matter boilerplate from title clues, gather bounded candidate summaries from trusted search results, and allow high-similarity title matches only with independent corroboration. Test noisy headings and conflicting papers.
3. Outcome detail: distinguish source unavailable, no candidate, ambiguous candidates, and evidence conflict; persist safe candidate summaries; render reasons and official-source links in the import job view. Test API and browser output.
4. Validation: run the full Python suite and browser management test, then verify existing paper metadata remains unchanged and active imports are idle before restarting the service.
