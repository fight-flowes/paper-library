# Paper Library Project Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the local paper reader reproducible, model-transparent, incrementally cached, recoverable after lenient translations, and safe under concurrent ingestion.

**Architecture:** Keep reading available without model configuration; isolate translation configuration and cache metadata behind `TranslationConfig` and `TranslationService`. Use one selected model per page, stable segment identities, cache quality metadata, an explicit warning retry mode, and a per-library publish lock.

**Tech Stack:** Python 3.11+, Flask, SQLite, markdown-it-py, httpx, pytest, Playwright, Bash

**Spec:** `docs/superpowers/specs/2026-09-19-project-hardening-design.md`

## Global Constraints

- Keep the reader bound to `127.0.0.1:18020`.
- Never expose model credentials to browser responses or logs.
- Preserve the read-only browser translation endpoint.
- Preserve existing Markdown and PDF files.
- Keep translation concurrency bounded to 1–32.

---

### Task 1: Optional and hermetic model configuration

**Files:**
- Modify: `reader/translation.py`
- Modify: `reader/app.py`
- Modify: `scripts/translate.py`
- Modify: `tests/test_translation.py`
- Modify: `tests/test_reader_app.py`
- Modify: `tests/test_translation_batch.py`

**Interfaces:**
- Produces: `TranslationConfig.from_environment(profile=None, required=True)`; `required=False` returns a disabled config instead of raising.

- [x] Add tests proving the reader starts without `.env`, invalid optional configuration stays disabled, and a real translation command still rejects missing configuration.
- [x] Run the focused tests and confirm they fail because configuration is currently mandatory.
- [x] Add optional configuration loading and make all tests use temporary settings files.
- [x] Run the focused tests and confirm they pass.

### Task 2: Strict model provenance and stable cache

**Files:**
- Modify: `reader/translation.py`
- Modify: `reader/translation_routes.py`
- Modify: `reader/translation_segments.py`
- Modify: `tests/test_translation.py`
- Modify: `tests/test_reader_app.py`

**Interfaces:**
- Produces: cache records containing text, model, profile, prompt fingerprint, request style, warnings, finish reason, and timestamps.
- Produces: stable segment IDs based on content occurrence rather than global token position.

- [x] Add tests proving the reader does not fall back to another model, unchanged paragraphs survive unrelated edits, and prompt/request-style changes isolate cache entries.
- [x] Run the tests and confirm each new behavior fails.
- [x] Add an idempotent SQLite schema migration, stable segment IDs, prompt fingerprinting, and strict selected-model reads.
- [x] Run translation and reader tests and confirm they pass.

### Task 3: Retry warning translations

**Files:**
- Modify: `reader/translation.py`
- Modify: `reader/translation_batch.py`
- Modify: `scripts/translate.py`
- Modify: `tests/test_translation_batch.py`

**Interfaces:**
- Produces: `--retry-warnings`; `TranslationService.cached_record(...)` exposes quality metadata; translation writes atomically replace warning records.

- [x] Add a batch test where a cached warning is skipped normally and retranslated with `retry_warnings=True`.
- [x] Run the focused test and confirm it fails because warnings are always treated as complete.
- [x] Implement warning-aware task selection and CLI reporting.
- [x] Run the focused and translation suites.

### Task 4: Concurrent parser publication lock

**Files:**
- Modify: `scripts/parse_pdf.py`
- Modify: `tests/test_parse_pdf.py`

**Interfaces:**
- Produces: a nonblocking `.catalog.lock` held during ID allocation and final catalog publication, with a clear `WorkflowError` for a competing publisher.

- [x] Add a process-level lock contention test.
- [x] Run it and confirm the second publisher is currently admitted.
- [x] Implement the library lock and keep MinerU staging cleanup intact.
- [x] Run parser tests.

### Task 5: Frontend state and warning presentation

**Files:**
- Modify: `reader/static/translation.js`
- Modify: `reader/static/reader.css`
- Modify: `tests/test_translation_browser.cjs`

**Interfaces:**
- Full translation hides status; partial/empty/error status remains visible; `.translation-warning` is visually distinct.

- [x] Update browser assertions for visible partial state and warning styling.
- [x] Run browser tests and confirm the partial-state assertion fails.
- [x] Fix visibility and add warning styles.
- [x] Run browser tests at desktop and mobile widths.

### Task 6: Documentation, reproducibility, and Git migration

**Files:**
- Modify: `README.md`
- Modify: `.gitignore`
- Create: `requirements-dev.txt`
- Stage: prior `paper/{reader,scripts,tests,requirements-reader.txt}` deletion and `paper/paper-library` replacement.

**Interfaces:**
- Produces: one accurate setup path using `.env.example`, pinned development test dependencies, and a reviewable Git rename/migration.

- [x] Rewrite contradictory translation, model, cache, warning and concurrency documentation.
- [x] Add development test dependencies and ignore runtime artifacts.
- [x] Run the full Python suite with the real `.env` and with a missing settings file.
- [x] Run the browser integration suite.
- [x] Stage only the paper project migration paths and inspect the staged diff summary for unrelated files.
