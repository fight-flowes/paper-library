# Review remediation implementation plan

Goal: resolve the reviewed reliability, deployment, search and maintainability issues while preserving the local single-user workflow and existing papers.
Architecture: modular monolith; durable publication journal, supervised worker, explicit parser environment; shared configuration/document model and model concurrency gate. Existing IDs and data remain compatible.
Spec: docs/maintenance/2026-09-22-project-review.md (user approved implementation).
Execution: inline, sequential changes and independent test batches; no unrelated worktree reset or automatic commit.

- [x] Publication transaction journal, durable recovery and failure injection tests.
- [x] Worker supervision/status, parser resolution and deadlines, actual service check.
- [x] Backup coordination/status/mirror verification and history recovery.
- [x] One dotenv parser and read/write-aware CLI guards.
- [x] Paper-level search pagination, per-paper incremental index and background refresh.
- [x] Shared annotated document structure for guide generation and existing-paper reconciliation.
- [x] Cross-process model budgets, consistent generation provenance.
- [x] Reading-position concurrency and save feedback.
- [x] Consolidate frontend rules, unified checks/CI/dependency lock and current documentation.
- [x] Full regression, small real parser smoke test, live service deployment and validation record.

Review focus: interruption at any file replacement; database outages inside error handling; empty/missing optional model data; existing notes and translated segments after structure changes; high-hit papers not hiding other search matches.

Implementation and acceptance evidence: `docs/maintenance/2026-09-22-review-remediation.md`. Off-disk mirror destination and activation of CI in the outer multi-project repository remain deployment configuration, not silently enabled services.
