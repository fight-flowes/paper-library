# Paper Library Modular Monolith Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorganize Paper Library into an installable modular monolith with three reliable command controllers, normalized ID-based paper storage, and lossless migration of papers and translations.

**Architecture:** Application code lives under `src/paper_library` and is split into catalog, ingest, metadata, translation, and web modules. Data lives under `libraries/<library-id>/papers/<paper-id>`, while extensionless Shell controllers provide environment and process orchestration. A tested one-time migrator converts the existing data tree and absolute-path translation identities before the old layout is removed.

**Tech Stack:** Python 3.11+, Flask, SQLite, httpx, markdown-it-py, Bash, pytest, Playwright, TOML

**Spec:** `docs/superpowers/specs/2026-09-19-paper-library-modular-monolith-design.md`

## Global Constraints

- Keep the project in the existing `paper-library/` workspace directory.
- Keep the reader bound to `127.0.0.1:18020`.
- Preserve all paper IDs, aliases, browser routes, source bytes, metadata, and current DeepSeek translations.
- Do not retain compatibility wrappers for `reader.sh`, `parse_pdf.sh`, or `translate.sh`.
- Never expose model credentials to browser responses, logs, or reports.
- Keep translation concurrency bounded to 1–32.
- Do not remove old data or cache files until the new layout passes verification.
- Do not commit during execution because the parent repository already contains a large staged migration; stage only the final scoped result.

---

### Task 1: Package foundation and centralized configuration

**Files:**
- Create: `pyproject.toml`
- Create: `src/paper_library/__init__.py`
- Create: `src/paper_library/config.py`
- Modify: `tests/conftest.py`
- Create: `tests/unit/test_config.py`

**Interfaces:**
- Produces: `ProjectPaths.from_root(root: Path) -> ProjectPaths`
- Produces: `Settings.load(paths: ProjectPaths, environ: Mapping[str, str] | None = None) -> Settings`
- Produces: `Settings.translation(profile: str | None = None, required: bool = True) -> TranslationSettings`

- [ ] Add failing tests for project paths, `.env` precedence, missing optional model configuration, custom Conda environment, state directory, library directory, host, and port.
- [ ] Run `conda run -n paper python -m pytest -q tests/unit/test_config.py` and confirm imports fail.
- [ ] Add `pyproject.toml` with the existing runtime dependencies, a `dev` extra containing pytest and Playwright test prerequisites, Hatch `src` packaging, pytest settings, and package data for templates/static files.
- [ ] Implement immutable configuration dataclasses without reading environment variables at module import time.
- [ ] Update the autouse test fixture so every test gets an isolated settings file, cache, state directory, and library root.
- [ ] Install the project editable with `conda run -n paper python -m pip install -e '.[dev]'` and run the focused tests.

### Task 2: Stable library model and normalized paths

**Files:**
- Create: `src/paper_library/catalog/__init__.py`
- Create: `src/paper_library/catalog/models.py`
- Create: `src/paper_library/catalog/repository.py`
- Create: `src/paper_library/catalog/paths.py`
- Create: `tests/unit/test_catalog.py`
- Create: `tests/integration/test_library_discovery.py`

**Interfaces:**
- Produces: `LibraryConfig(id: str, name: str, paper_id_prefix: str)`
- Produces: `LibraryRepository(root: Path)` with `discover()`, `load(library_id)`, `find_paper(library_id, paper_id)`, and `validate_all()`.
- Produces: `PaperPaths.for_id(library_root: Path, paper_id: str) -> PaperPaths` with fixed `source.pdf`, `article.md`, `metadata.json`, and `images/` paths.

- [ ] Add failing tests for `library.toml`, normalized catalog headers, directory-ID agreement, aliases, cross-library duplicate canonical IDs, deterministic paper paths, and symlink/path escape rejection.
- [ ] Confirm the tests fail because the new package does not exist.
- [ ] Move catalog dataclasses and path guards from `reader/library.py` into focused modules and remove `source_pdf`/`markdown` from the in-memory record.
- [ ] Implement discovery under `libraries/` only; require both `library.toml` and `catalog.tsv`.
- [ ] Implement whole-project validation that reports missing PDF, Markdown, metadata and images without hiding other libraries.
- [ ] Run catalog and discovery tests.

### Task 3: Repackage the web application and add readiness

**Files:**
- Create: `src/paper_library/web/__init__.py`
- Move: `reader/app.py` to `src/paper_library/web/app.py`
- Move: `reader/markdown_renderer.py` to `src/paper_library/web/markdown.py`
- Move: `reader/templates/` to `src/paper_library/web/templates/`
- Move: `reader/static/` to `src/paper_library/web/static/`
- Create: `src/paper_library/web/health.py`
- Modify: `tests/test_reader_app.py`
- Modify: `tests/test_markdown_renderer.py`
- Create: `tests/integration/test_health.py`

**Interfaces:**
- Produces: `create_app(settings: Settings | None = None, *, repository: LibraryRepository | None = None) -> Flask`
- Produces: `python -m paper_library.web.app`
- Produces: `GET /health/live` and `GET /health/ready`.

- [ ] Change tests to import `paper_library.web` and add failing readiness tests for valid libraries and a broken catalog.
- [ ] Move the web files, configure Flask package resource folders explicitly, and inject `Settings` plus `LibraryRepository` into the app.
- [ ] Keep `/library/<library-id>/...` and legacy `/paper/<paper-id>` redirect behavior unchanged.
- [ ] Make readiness return structured JSON with library counts and warnings, without secrets or model calls.
- [ ] Run web, renderer, and health tests.

### Task 4: Repackage translation and use stable identities

**Files:**
- Create: `src/paper_library/translation/__init__.py`
- Move and split: `reader/translation*.py` into `src/paper_library/translation/`
- Create: `src/paper_library/translation/cli.py`
- Create: `src/paper_library/translation/migrate.py`
- Modify: `tests/test_translation.py`
- Modify: `tests/test_translation_batch.py`
- Create: `tests/integration/test_translation_cache_migration.py`

**Interfaces:**
- Produces: `paper_identity(library_id: str, paper_id: str) -> str` returning `"<library-id>/<paper-id>"`.
- Produces: `TranslationService` using stable identity in `_key`.
- Produces: `migrate_cache(old_cache: Path, new_cache: Path, mappings: Iterable[IdentityMapping]) -> MigrationReport`.
- Produces: CLI commands `check`, `status`, `plan`, `run`, and `retry-warnings`.

- [ ] Add failing tests showing cache reuse after physical library moves, correct range selection, and lossless migration of text, model, prompt, warnings, finish reason, and timestamp.
- [ ] Move renderer segments, service, batch, routes, and progress into the package while preserving the current request and validation behavior.
- [ ] Replace absolute library paths in translation tasks with stable library IDs.
- [ ] Implement a transactional, idempotent cache migrator with collision detection and a JSON report.
- [ ] Implement the Python CLI with mutually exclusive `--all` and `--library [--paper]` selection.
- [ ] Run all translation tests and a migration test twice against the same temporary database.

### Task 5: Split ingest and metadata workflows

**Files:**
- Create: `src/paper_library/metadata/__init__.py`
- Move and split: `scripts/paper_metadata.py` into `src/paper_library/metadata/`
- Create: `src/paper_library/ingest/__init__.py`
- Split: `scripts/parse_pdf.py` into `src/paper_library/ingest/models.py`, `mineru.py`, `publisher.py`, `workflow.py`, and `cli.py`
- Modify: `tests/test_paper_metadata.py`
- Modify: `tests/test_parse_pdf.py`
- Create: `tests/integration/test_ingest_transaction.py`

**Interfaces:**
- Produces: `IngestRequest(library_id: str, source_pdf: Path, subject: str, enrich: bool)`.
- Produces: `IngestService.ingest(request: IngestRequest) -> PaperRecord`.
- Produces: `MetadataService.refresh(library_id: str, paper_id: str) -> MetadataRecord`.
- Produces: library CLI commands `list`, `check`, `show`, `ingest`, and `refresh-metadata`.

- [ ] Update tests to target the package and add failing tests for arbitrary input paths, duplicate SHA reuse, ID-based output, atomic publication, and interrupted staging cleanup.
- [ ] Move metadata HTTP matching and override logic without changing network privacy or timeouts.
- [ ] Extract MinerU execution, identity allocation, and publisher transaction from the old 893-line script.
- [ ] Publish fixed paper filenames and the normalized catalog under one library lock.
- [ ] Implement the Python library CLI and run ingest/metadata tests.

### Task 6: Public Shell controllers and safe process ownership

**Files:**
- Create: `scripts/_common`
- Create: `scripts/dev`
- Create: `scripts/dev_process.py`
- Create: `scripts/library`
- Create: `scripts/translate`
- Create: `tests/scripts/test_common_script.py`
- Replace: `tests/test_reader_startup.py` with `tests/scripts/test_dev_script.py`
- Create: `tests/scripts/test_library_script.py`
- Create: `tests/scripts/test_translate_script.py`

**Interfaces:**
- `scripts/dev <up|stop|restart|status|logs|urls|run|doctor>`
- `scripts/library <list|check|show|ingest|refresh-metadata>`
- `scripts/translate <check|status|plan|run|retry-warnings>`

- [ ] Add failing controller tests using fake Conda/Python processes and unused ports.
- [ ] Implement `_common` with project root discovery, `PAPER_LIBRARY_CONDA_ENV` defaulting to `paper`, `run_in_env`, and consistent exit code 2 for usage/configuration errors.
- [ ] Implement reader launch with `start_new_session=True`, PID/log state, HTTP readiness wait, and verified process ownership based on user, process group, cwd, module, host and port.
- [ ] Implement `stop` recovery for a missing PID file and refusal to stop a foreign listener.
- [ ] Add the thin library and translation controllers with no personal absolute paths.
- [ ] Run all script tests and `bash -n scripts/_common scripts/dev scripts/library scripts/translate`.

### Task 7: Migrate real libraries and translation cache

**Files:**
- Create: `src/paper_library/migration.py`
- Create: `tests/integration/test_project_migration.py`
- Move: `Mechanism Interpretability/` to `libraries/mechanism-interpretability/`
- Move: `Whitespace/` to `libraries/whitespace/`
- Modify: both catalogs and metadata files

**Interfaces:**
- Produces: `migrate_project(old_root: Path, new_root: Path, cache: Path, *, apply: bool) -> ProjectMigrationReport`.

- [ ] Build a compact old-layout fixture and add a failing test that asserts byte-for-byte source preservation, normalized catalogs, stable IDs, aliases, idempotency, and translated segment counts.
- [ ] Implement dry-run planning and apply mode using `libraries.new/`, fsync/rename publication, and detailed JSON reporting.
- [ ] Stop the running reader and back up the real cache.
- [ ] Run a dry-run against the real project and require six papers, two libraries, 1283 current translations, and three warning records.
- [ ] Apply the data and cache migration, then rerun it in dry-run mode to prove no pending changes.
- [ ] Verify SHA256 for all PDFs, byte equality for all Markdown, image set equality, metadata validity, and translation counts before removing the old trees.

### Task 8: Remove legacy layout, update documentation, and verify

**Files:**
- Delete: `reader/`
- Delete: `scripts/*.sh`, `scripts/parse_pdf.py`, `scripts/paper_metadata.py`, `scripts/translate.py`
- Delete: `requirements-reader.txt`, `requirements-dev.txt`
- Modify: `.gitignore`
- Rewrite: `README.md`
- Reorganize: `tests/` into unit/integration/scripts/browser where imports benefit from the grouping

**Interfaces:**
- Leaves exactly three public executable entrypoints: `scripts/dev`, `scripts/library`, `scripts/translate`.

- [ ] Remove legacy modules and wrappers after all package imports are in use; scan for obsolete paths and commands with `rg`.
- [ ] Document installation via `pip install -e '.[dev]'`, the new tree, all command help, configuration, data invariants, migration backup, and recovery.
- [ ] Run `conda run -n paper python -m pytest -q` with isolated settings and again with an explicitly missing settings file.
- [ ] Run the Playwright browser integration at desktop and mobile widths.
- [ ] Run `scripts/dev doctor`, real `scripts/translate check`, `scripts/library check`, `scripts/translate plan --all`, and start the reader with `scripts/dev up`.
- [ ] Verify HTTP 200 for readiness, home, both libraries, every paper, PDFs, representative images, and translation endpoints.
- [ ] Confirm the all-library plan reports 1283 cached, zero missing, and three warnings.
- [ ] Stage only `paper/paper-library` and the already authorized old paper project paths; inspect the staged summary and scan for secrets/runtime artifacts.
