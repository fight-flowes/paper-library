# Web PDF Ingest and Translation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a local user upload a PDF, choose a library and subject, parse it with MinerU, optionally translate it, monitor durable progress, and open the published paper entirely through the browser.

**Architecture:** Add a SQLite-backed job subsystem inside the existing Flask modular monolith. A single background coordinator claims durable jobs, invokes the transactional ingest service, and then uses the existing bounded translation batch; management routes and compact polling pages expose the workflow without adding an external queue service.

**Tech Stack:** Python 3.11+, Flask 3, SQLite, `threading`, `subprocess`, MinerU, httpx, Markdown/Jinja, vanilla JavaScript, pytest, Playwright

**Spec:** `docs/superpowers/specs/2026-09-19-web-ingest-translation-design.md`

## Global Constraints

- Keep the application as one installable modular monolith under `src/paper_library`.
- Keep runtime jobs and staged uploads beneath ignored `.paper-library-dev/`.
- Permit exactly one active MinerU workflow; translation inside that workflow remains bounded by the selected model's configured concurrency, currently 16.
- Accept one PDF per job, default maximum 256 MiB, requiring both a `.pdf` suffix and `%PDF-` signature.
- Accept library IDs only from `LibraryRepository.discover()` and normalize subjects to one line of at most 120 Unicode characters.
- Enable automatic translation by default, while allowing parse-only submission and later translation.
- Preserve terminal commands, fixed paper package paths, stable `library-id/paper-id` translation identities, and the read-only paper translation endpoint.
- Never expose credentials, absolute paths, model response bodies, or uncontrolled subprocess output through HTML, JSON, reports, or logs.
- Disable management mutations on a non-loopback bind unless `PAPER_LIBRARY_ENABLE_REMOTE_MANAGEMENT=1` is explicitly configured.
- Use POST/Redirect/GET plus a persistent random management token for every mutation.
- Do not modify the six existing papers during automated tests or real verification.
- The parent repository contains an existing staged migration; stage only scoped Paper Library files and do not create commits during execution.

## Review Focus

- A duplicate PDF submitted twice or resumed after publication must resolve to one stable paper rather than allocate a second ID; Task 4 adds this workflow test.
- A restart after package replacement but before job completion must reclaim the job and resume from catalog/cache state; Tasks 1 and 4 add lease and recovery tests.
- An oversized stream without a trustworthy `Content-Length` must stop once the byte limit is crossed and remove its partial upload; Task 2 adds a streaming-limit test.
- A library removed between form rendering and job execution must fail the job with a sanitized message while retaining the upload for retry; Task 4 adds this race test.
- Translation model failure after successful publication must preserve the paper and retry translation without rerunning MinerU; Task 4 adds a phase-resume test.

---

### Task 1: Runtime Configuration and Durable Job Repository

**Files:**
- Modify: `src/paper_library/config.py`
- Create: `src/paper_library/jobs/__init__.py`
- Create: `src/paper_library/jobs/models.py`
- Create: `src/paper_library/jobs/repository.py`
- Create: `tests/unit/test_job_repository.py`
- Modify: `tests/unit/test_config.py`

**Interfaces:**
- Consumes: `Settings.paths.state`, existing dotenv precedence, SQLite from the standard library.
- Produces: `JobState`, `JobStage`, `JobRecord`, `NewJob`, and `JobProgress` dataclasses.
- Produces: `JobRepository(database: Path)` with `create`, `get`, `recent`, `claim_next`, `heartbeat`, `update`, `request_cancel`, `cancel_requested`, `retry`, `complete`, `fail`, and `recover_expired`.
- Produces: `Settings.upload_max_bytes`, `Settings.remote_management_enabled`, `Settings.jobs_database`, and `Settings.uploads_directory`.

- [ ] **Step 1: Add failing configuration tests**

```python
def test_management_settings_have_safe_defaults(tmp_path):
    settings = Settings.load(ProjectPaths.from_root(tmp_path), {})
    assert settings.upload_max_bytes == 256 * 1024 * 1024
    assert not settings.remote_management_enabled
    assert settings.jobs_database == tmp_path.resolve() / ".paper-library-dev/jobs.sqlite3"
    assert settings.uploads_directory == tmp_path.resolve() / ".paper-library-dev/uploads"

def test_upload_limit_and_remote_management_are_configurable(tmp_path):
    settings = Settings.load(ProjectPaths.from_root(tmp_path), {
        "PAPER_LIBRARY_UPLOAD_MAX_MB": "32",
        "PAPER_LIBRARY_ENABLE_REMOTE_MANAGEMENT": "1",
    })
    assert settings.upload_max_bytes == 32 * 1024 * 1024
    assert settings.remote_management_enabled
```

- [ ] **Step 2: Run the configuration tests and confirm the new attributes are missing**

Run: `conda run -n paper pytest -q tests/unit/test_config.py`

Expected: FAIL with `AttributeError` for `upload_max_bytes`.

- [ ] **Step 3: Implement strict management configuration**

Add validated fields to `Settings`; accept `PAPER_LIBRARY_UPLOAD_MAX_MB` from 1 through 4096 and accept the remote flag only as `0` or `1`:

```python
upload_mb = int(values.get("PAPER_LIBRARY_UPLOAD_MAX_MB", "256"))
if not 1 <= upload_mb <= 4096:
    raise ValueError("PAPER_LIBRARY_UPLOAD_MAX_MB 必须在 1 到 4096 之间")
remote_raw = values.get("PAPER_LIBRARY_ENABLE_REMOTE_MANAGEMENT", "0")
if remote_raw not in {"0", "1"}:
    raise ValueError("PAPER_LIBRARY_ENABLE_REMOTE_MANAGEMENT 必须为 0 或 1")
```

- [ ] **Step 4: Add failing job-model and transition tests**

```python
def test_repository_claims_one_job_and_persists_progress(tmp_path):
    repository = JobRepository(tmp_path / "jobs.sqlite3")
    job = repository.create(NewJob(
        original_filename="paper.pdf", library_id="whitespace",
        subject="related-work", language="en", enrich=True,
        auto_translate=True, upload_name="job/source.pdf",
    ))
    claimed = repository.claim_next("runner-a", lease_seconds=30)
    assert claimed.id == job.id
    assert repository.claim_next("runner-b", lease_seconds=30) is None
    repository.update(job.id, JobStage.TRANSLATING,
                      JobProgress(total=10, cached=2, translated=3, failed=0))
    assert repository.get(job.id).progress.translated == 3

def test_expired_job_is_recovered_without_losing_paper_id(tmp_path, clock):
    repository = JobRepository(tmp_path / "jobs.sqlite3", clock=clock)
    job = repository.create(new_job())
    repository.claim_next("dead-runner", lease_seconds=1)
    repository.update(job.id, JobStage.TRANSLATING, JobProgress(total=5), paper_id="ws-p0003")
    clock.advance(2)
    assert repository.recover_expired() == 1
    recovered = repository.get(job.id)
    assert recovered.state is JobState.QUEUED
    assert recovered.paper_id == "ws-p0003"
```

- [ ] **Step 5: Run the repository tests and confirm imports fail**

Run: `conda run -n paper pytest -q tests/unit/test_job_repository.py`

Expected: FAIL with `ModuleNotFoundError: paper_library.jobs`.

- [ ] **Step 6: Implement immutable job models and explicit transition rules**

Use string enums for serialization and reject transitions outside this table:

```python
ALLOWED_TRANSITIONS = {
    JobState.QUEUED: {JobState.RUNNING, JobState.CANCELLED},
    JobState.RUNNING: {JobState.QUEUED, JobState.COMPLETED,
                       JobState.COMPLETED_WITH_WARNINGS,
                       JobState.FAILED, JobState.CANCELLED},
    JobState.FAILED: {JobState.QUEUED, JobState.CANCELLED},
    JobState.COMPLETED: set(),
    JobState.COMPLETED_WITH_WARNINGS: {JobState.QUEUED},
    JobState.CANCELLED: set(),
}
```

Define `JobProgress(total=0, cached=0, translated=0, failed=0, pending=0)` and keep `error` to one sanitized string of at most 500 characters.

- [ ] **Step 7: Implement the SQLite repository with transactional claiming**

Create schema version 1 in a `schema` table. Use `BEGIN IMMEDIATE` for `claim_next`, assign `runner_id`, `lease_expires_at`, and state `running` in the same transaction, and use WAL plus a 10-second busy timeout. Store booleans as integers and timestamps as UTC ISO-8601 strings.

- [ ] **Step 8: Add tests for invalid transitions, cancellation, retry, claim races, bounded recent history, and expired-lease recovery**

Include two repository instances against the same database and assert only one claims the queued row. Assert retry clears the error and progress but retains `paper_id` and `upload_name`.

- [ ] **Step 9: Run the focused tests**

Run: `conda run -n paper pytest -q tests/unit/test_config.py tests/unit/test_job_repository.py`

Expected: PASS.

---

### Task 2: Secure Upload Staging and Management Guard

**Files:**
- Create: `src/paper_library/jobs/uploads.py`
- Create: `src/paper_library/jobs/security.py`
- Create: `tests/unit/test_job_uploads.py`
- Create: `tests/unit/test_management_security.py`

**Interfaces:**
- Consumes: `Settings.upload_max_bytes`, `Settings.uploads_directory`, `LibraryRepository.discover()`, `werkzeug.datastructures.FileStorage`.
- Produces: `UploadRequest(library_id, subject, language, enrich, auto_translate)`.
- Produces: `UploadStager.stage(file: FileStorage, request: UploadRequest) -> StagedUpload` and `UploadStager.remove(upload_name: str) -> None`.
- Produces: `ManagementGuard.from_state_directory(path: Path, host: str, remote_enabled: bool)`, `.token`, `.validate(token)`, and `.mutations_enabled`.

- [ ] **Step 1: Write failing validation and streaming-limit tests**

```python
def test_stage_rejects_stream_larger_than_limit_and_removes_partial(tmp_path):
    stager = UploadStager(tmp_path / "uploads", max_bytes=8)
    file = FileStorage(stream=BytesIO(b"%PDF-123456789"), filename="paper.pdf")
    with pytest.raises(UploadError, match="过大"):
        stager.stage(file, valid_request())
    assert list((tmp_path / "uploads").glob("**/*")) == []

@pytest.mark.parametrize("filename,payload", [
    ("paper.txt", b"%PDF-content"),
    ("paper.pdf", b"not-a-pdf"),
])
def test_stage_requires_suffix_and_signature(tmp_path, filename, payload):
    with pytest.raises(UploadError):
        make_stager(tmp_path).stage(FileStorage(BytesIO(payload), filename=filename), valid_request())
```

- [ ] **Step 2: Run upload tests and confirm the module is missing**

Run: `conda run -n paper pytest -q tests/unit/test_job_uploads.py`

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement request validation and bounded streaming**

Normalize subject with:

```python
subject = re.sub(r"[\t\r\n]+", " ", raw_subject).strip()
if not subject or len(subject) > 120:
    raise UploadError("主题必须为 1 到 120 个字符")
if language not in {"en", "ch"}:
    raise UploadError("不支持的文档语言")
if library_id not in repository.discover():
    raise UploadError("目标论文库不存在")
```

Write 1 MiB chunks to `<uploads>/.<uuid>.tmp/source.pdf`, stop immediately after `max_bytes`, verify the first five bytes, then atomically rename the directory to `<uploads>/<uuid>/`. Return only the relative `<uuid>/source.pdf` storage name.

- [ ] **Step 4: Write failing management-token tests**

```python
def test_guard_persists_private_token_and_uses_constant_time_compare(tmp_path):
    first = ManagementGuard.from_state_directory(tmp_path, "127.0.0.1", False)
    second = ManagementGuard.from_state_directory(tmp_path, "127.0.0.1", False)
    assert first.token == second.token
    assert first.validate(first.token)
    assert not first.validate("wrong")
    assert stat.S_IMODE((tmp_path / "management.token").stat().st_mode) == 0o600

def test_remote_bind_disables_mutations_without_explicit_opt_in(tmp_path):
    assert not ManagementGuard.from_state_directory(tmp_path, "0.0.0.0", False).mutations_enabled
    assert ManagementGuard.from_state_directory(tmp_path, "0.0.0.0", True).mutations_enabled
```

- [ ] **Step 5: Implement the persistent management guard**

Generate 32 random bytes with `secrets.token_urlsafe(32)`, create the file with mode `0o600`, verify existing file ownership and regular-file status, and compare submitted tokens using `secrets.compare_digest`. Treat IPv4 and IPv6 loopback addresses as local using `ipaddress.ip_address(host).is_loopback`; the hostname `localhost` is also local.

- [ ] **Step 6: Add tests for unknown libraries, empty/multiline/overlong subjects, unsupported languages, hostile filenames, cleanup, and non-loopback binds**

Assert the staged path never contains the supplied filename and cannot escape the uploads root.

- [ ] **Step 7: Run both focused suites**

Run: `conda run -n paper pytest -q tests/unit/test_job_uploads.py tests/unit/test_management_security.py`

Expected: PASS.

---

### Task 3: Structured and Cancellable Ingestion

**Files:**
- Modify: `src/paper_library/ingest/service.py`
- Modify: `src/paper_library/cli/library.py`
- Create: `tests/unit/test_ingest_service.py`
- Modify: `tests/scripts/test_commands.py`

**Interfaces:**
- Consumes: normalized library catalog and package layout from `paper_library.catalog`.
- Produces: `IngestOutcome` enum and `IngestResult(library_id, paper_id, outcome, paths)`.
- Produces: `MinerURunner.run(source, output, language, *, cancelled, on_event) -> None`.
- Changes: `ingest_pdf(..., cancelled: Callable[[], bool] = never, on_event: Callable[[str], None] = ignore) -> IngestResult`.

- [ ] **Step 1: Add failing tests for structured results and duplicate SHA reuse**

```python
def test_duplicate_pdf_returns_existing_paper_without_running_mineru(library, pdf):
    runner = Mock()
    result = ingest_pdf(library.repository, "whitespace", pdf,
                        mineru_runner=runner, enrich=False)
    assert result == IngestResult("whitespace", "ws-p0001", IngestOutcome.UNCHANGED,
                                  PaperPaths.for_id(library.root, "ws-p0001"))
    runner.run.assert_not_called()
```

- [ ] **Step 2: Add a failing transaction rollback test**

Inject a catalog publisher that raises after package replacement. Assert the previous package bytes and previous catalog bytes are restored, no `.backup` or `.paper-staging-*` path remains, and `LibraryRepository.load()` still succeeds.

- [ ] **Step 3: Run the ingest tests and confirm the current string return type fails**

Run: `conda run -n paper pytest -q tests/unit/test_ingest_service.py`

Expected: FAIL comparing a string with `IngestResult`.

- [ ] **Step 4: Introduce structured results and injectable collaborators**

Define:

```python
class IngestOutcome(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"

@dataclass(frozen=True, slots=True)
class IngestResult:
    library_id: str
    paper_id: str
    outcome: IngestOutcome
    paths: PaperPaths
```

Keep terminal presentation in `cli/library.py`, mapping outcomes to `已导入`, `已更新`, or `未变更` after the service returns.

- [ ] **Step 5: Implement `MinerURunner` with owned process groups and bounded events**

Start MinerU with `start_new_session=True`, merge stderr into stdout, retain at most the last 200 sanitized lines, and call `on_event` no more than four times per second. While reading output, check `cancelled()`; on cancellation send `SIGTERM` to the child process group, wait up to five seconds, then send `SIGKILL` only to the same group.

- [ ] **Step 6: Make publication recoverable**

Before replacement, write both staged package and staged catalog. Rename the current package and catalog to unique backups, publish both, validate the new snapshot contains `paper_id`, then remove backups. On any exception, remove partially published targets and restore both backups. Preserve the library `flock` for the whole allocation and publication window.

- [ ] **Step 7: Add cancellation, invalid MinerU output, changed-source force, metadata outage, and rollback tests**

Use a fake runner that writes one Markdown and images for success, raises for failure, and observes a test cancellation event. Make the metadata gateway unavailable and assert publication still succeeds with `enrichment.status == "unavailable"`. Verify staged directories are always removed.

- [ ] **Step 8: Update CLI tests for structured outcome formatting**

Patch `ingest_pdf` to return each `IngestOutcome` and assert `scripts/library ingest` prints a stable paper identity without exposing temporary paths.

- [ ] **Step 9: Run ingest and command tests**

Run: `conda run -n paper pytest -q tests/unit/test_ingest_service.py tests/scripts/test_commands.py`

Expected: PASS.

---

### Task 4: Job Workflow, Translation Resume, and Background Runner

**Files:**
- Create: `src/paper_library/jobs/workflow.py`
- Create: `src/paper_library/jobs/runner.py`
- Create: `tests/unit/test_job_workflow.py`
- Create: `tests/unit/test_job_runner.py`

**Interfaces:**
- Consumes: `JobRepository`, `UploadStager`, structured `ingest_pdf`, `load_tasks`, `run_batch`, `TranslationService`.
- Produces: `JobWorkflow.run(job: JobRecord, cancelled: Callable[[], bool]) -> None`.
- Produces: `JobRunner(repository, workflow_factory, *, lease_seconds=30, poll_seconds=0.5)` with `start`, `stop`, `wake`, and `cancel`.

- [ ] **Step 1: Write a failing parse-only workflow test**

```python
def test_parse_only_job_publishes_and_completes(job_context):
    job = job_context.create(auto_translate=False)
    job_context.workflow.run(job, cancelled=lambda: False)
    saved = job_context.jobs.get(job.id)
    assert saved.state is JobState.COMPLETED
    assert saved.stage is JobStage.COMPLETED
    assert saved.paper_id == "ws-p0003"
    job_context.translator.assert_not_called()
    assert not job_context.upload_path.exists()
```

- [ ] **Step 2: Write failing translation-resume and partial-failure tests**

Cover a job with `paper_id` already set: assert MinerU is not invoked, `load_tasks` uses that paper, cached counts are preserved, zero failures completes normally, and nonzero segment failures complete as `COMPLETED_WITH_WARNINGS`.

- [ ] **Step 3: Run workflow tests and confirm the module is missing**

Run: `conda run -n paper pytest -q tests/unit/test_job_workflow.py`

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 4: Implement phase-aware orchestration**

The workflow must choose its starting phase from persisted data:

```python
if job.paper_id is None:
    result = ingest_pdf(...)
    jobs.update(job.id, JobStage.PUBLISHED, paper_id=result.paper_id)
else:
    result = existing_ingest_result(job.library_id, job.paper_id)

if not job.auto_translate:
    jobs.complete(job.id)
    uploads.remove(job.upload_name)
    return

tasks = load_tasks(libraries, job.library_id, job.paper_id)
report = run_batch(tasks, translation_service, stop_event=cancel_event,
                   on_update=throttled_progress)
```

Map model/configuration failures after publication to `FAILED` while retaining `paper_id` and the upload until a retry decision. Remove the upload after completed or completed-with-warnings.

- [ ] **Step 5: Add review-focus workflow tests**

Add exact tests for duplicate submission, missing library at execution, expired-job resume after publication, and model failure followed by retry. In the last test assert the fake MinerU call count remains one and translation call count becomes two.

- [ ] **Step 6: Write failing runner claim, lease, wake, and shutdown tests**

Use a short poll interval and synchronization events. Assert two runners sharing one database execute a job once, a wake processes without waiting for the full interval, heartbeat prevents recovery, and `stop()` joins the thread without abandoning an active job lease.

- [ ] **Step 7: Implement the single-coordinator runner**

Use a daemon thread named `paper-jobs`, a unique `runner_id`, a `threading.Event` wake signal, and a per-active-job cancellation event. Claim one job at a time, heartbeat while workflow runs, catch all workflow exceptions into a sanitized `jobs.fail`, and recover expired leases at startup.

- [ ] **Step 8: Implement cancellation ownership**

`cancel(job_id)` first persists `cancel_requested`. If the requested ID is active in this runner, set only its cancellation event. A queued cancellation transitions immediately to cancelled and removes its upload; an active job reaches cancelled through the workflow cleanup path.

- [ ] **Step 9: Implement and test startup cleanup of orphaned uploads**

After lease recovery, compare direct child upload IDs with non-terminal jobs. Remove temporary directories and uploads belonging to completed or cancelled jobs, retain uploads referenced by queued/running/failed jobs, and ignore symlinks. Add a test with one directory in each category and assert only retryable job uploads remain.

- [ ] **Step 10: Run job workflow and runner tests**

Run: `conda run -n paper pytest -q tests/unit/test_job_workflow.py tests/unit/test_job_runner.py`

Expected: PASS without leaked threads.

---

### Task 5: Management Routes and Application Lifecycle

**Files:**
- Create: `src/paper_library/jobs/routes.py`
- Modify: `src/paper_library/web/app.py`
- Create: `src/paper_library/web/templates/import.html`
- Create: `src/paper_library/web/templates/job.html`
- Modify: `src/paper_library/web/templates/libraries.html`
- Create: `tests/integration/test_management_routes.py`

**Interfaces:**
- Consumes: repository, stager, guard, runner, library discovery, and existing Flask app factory.
- Produces: `create_management_blueprint(dependencies: JobDependencies) -> Blueprint`.
- Changes: `create_app(settings=None, *, job_runner=None, start_jobs=True) -> Flask` and stores `job_repository`, `job_runner`, `upload_stager`, and `management_guard` in `app.extensions`.

- [ ] **Step 1: Add a failing upload-through-Flask integration test**

```python
def test_upload_creates_job_and_redirects(client, management_token):
    response = client.post("/manage/jobs", data={
        "management_token": management_token,
        "library_id": "whitespace",
        "subject": "related-work",
        "language": "en",
        "auto_translate": "1",
        "pdf": (BytesIO(b"%PDF-fixture"), "paper.pdf"),
    }, content_type="multipart/form-data")
    assert response.status_code == 303
    assert re.search(r"/manage/jobs/[0-9a-f-]+$", response.headers["Location"])
```

- [ ] **Step 2: Add failing security and JSON-shape tests**

Assert missing/wrong tokens return 403, GET on mutation paths returns 405, remote-disabled management returns 403, and `/api/jobs/<id>` contains only an allowlist of ID, filename, library, subject, state, stage, paper URL, progress, error, and timestamps. Explicitly assert serialized JSON does not contain `/home/`, `API_KEY`, upload storage names, or model request text.

- [ ] **Step 3: Run route tests and confirm routes return 404**

Run: `conda run -n paper pytest -q tests/integration/test_management_routes.py`

Expected: FAIL because `/manage/import` and `/manage/jobs` do not exist.

- [ ] **Step 4: Implement the blueprint and POST/Redirect/GET flow**

On `POST /manage/jobs`, validate the management token before reading the upload, construct `UploadRequest`, stage the file, create `NewJob`, call `runner.wake()`, and return `redirect(url_for("management.job_detail", job_id=job.id), code=303)`. If job creation fails after staging, remove the staged upload before rendering the error.

- [ ] **Step 5: Implement retry, translate, and cancel mutations**

`translate` sets `auto_translate=True`, changes completed parse-only jobs back to queued, and wakes the runner. `retry` accepts only failed or completed-with-warnings jobs and preserves `paper_id`. `cancel` is idempotent for queued/running jobs. Every path validates the same token and redirects back to the job page.

- [ ] **Step 6: Implement allowlisted JSON serialization and no-store headers**

Generate `paper_url` with `url_for` only when `paper_id` exists. Apply `Cache-Control: no-store` to management HTML and API responses. Return 404 for unknown job IDs without revealing the jobs database path.

- [ ] **Step 7: Wire lifecycle dependencies into `create_app`**

Create production dependencies from `Settings`; inject a disabled fake runner in tests. Register the blueprint and start the real runner after all extensions are ready. Register `atexit` cleanup and make `JobRunner.start()` idempotent so Flask construction cannot create duplicate threads.

- [ ] **Step 8: Build server-rendered form and job fallback pages**

The import template must work without JavaScript and include the hidden token, library selector, subject input, language, enrichment, automatic-translation checkbox, upload limit, validation errors, and recent jobs. The job template must show persisted state and POST forms for the currently valid actions.

- [ ] **Step 9: Add persistence-across-app-recreation and missing-library race tests**

Create a job with one app, close its runner, recreate the app against the same state directory, and assert the job page and API retain the job. Delete the target library before executing a queued job and assert a sanitized failed state plus retained staged upload.

- [ ] **Step 10: Run route and existing reader integration tests**

Run: `conda run -n paper pytest -q tests/integration/test_management_routes.py tests/integration/test_reader.py`

Expected: PASS.

---

### Task 6: Live Progress UI and Responsive Styling

**Files:**
- Create: `src/paper_library/web/static/jobs.js`
- Modify: `src/paper_library/web/static/reader.css`
- Modify: `src/paper_library/web/templates/import.html`
- Modify: `src/paper_library/web/templates/job.html`
- Modify: `src/paper_library/web/templates/base.html`
- Create: `tests/browser/management.cjs`
- Modify: `package.json`

**Interfaces:**
- Consumes: management JSON endpoints and server-rendered fallbacks from Task 5.
- Produces: progressive subject suggestions, one dynamic stage display, one progress bar, and terminal-state polling shutdown.

- [ ] **Step 1: Add a failing Playwright management scenario**

Create a disposable test server with a fake MinerU runner and mock translator. The browser test must upload a small fixture PDF, select `Whitespace`, choose `related-work`, leave automatic translation enabled, observe parsing then translating, and follow the resulting paper link. Assert exactly one `.job-progress` element exists throughout.

- [ ] **Step 2: Run the new browser test and confirm the import action is absent**

Run: `PAPER_TEST_BROWSER=<chromium> npm run test:management`

Expected: FAIL locating the **导入论文** action.

- [ ] **Step 3: Implement subject suggestions without a new request**

Embed an allowlisted mapping with Jinja's `tojson`:

```html
<script type="application/json" id="library-subjects">{{ subjects_by_library | tojson }}</script>
```

When the library selector changes, replace only generated `<option>` children in the subject datalist and keep the user's current editable value.

- [ ] **Step 4: Implement bounded polling and progress rendering**

Poll the job URL immediately, then every second while queued/running. Update text with `textContent`, set progress using `value`/`max`, toggle valid server-rendered action forms, and stop polling for completed, completed-with-warnings, failed, or cancelled states. On fetch failure, back off to five seconds and retain the last known content.

- [ ] **Step 5: Add responsive management styles**

Use existing CSS variables. At desktop width use a two-column form grid; below 720 px use one column. Give status colors accessible contrast, keep the original filename wrapping, and ensure progress/actions do not create horizontal overflow at 390 px.

- [ ] **Step 6: Extend browser coverage**

Add parse-only submission followed by **开始翻译**, invalid-PDF form feedback, one simulated failure and retry, page reload during translation, automatic polling stop, zero browser requests containing model credentials, and mobile overflow checks.

- [ ] **Step 7: Run both browser suites**

Run: `PAPER_TEST_BROWSER=<chromium> npm run test:browser && PAPER_TEST_BROWSER=<chromium> npm run test:management`

Expected: PASS.

---

### Task 7: Configuration Template, Command Parity, and Documentation

**Files:**
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `src/paper_library/cli/dev.py`
- Modify: `tests/scripts/test_commands.py`
- Modify: `tests/unit/test_config.py`

**Interfaces:**
- Consumes: final management settings and job repository.
- Produces: documented web workflow and `scripts/dev doctor` job diagnostics.

- [ ] **Step 1: Add failing doctor-output tests**

Assert doctor JSON contains `jobs_database_accessible`, `queued_jobs`, `active_jobs`, `upload_max_mb`, and `management_mutations_enabled`, while recursively asserting no value contains the management token, API key, or absolute upload filename.

- [ ] **Step 2: Extend `scripts/dev doctor` with job diagnostics**

Open the repository, run schema initialization, count queued and active jobs, and report only booleans/counts. Return exit code 1 when the jobs database cannot be created or management is unexpectedly disabled on a loopback bind.

- [ ] **Step 3: Add management settings to `.env.example`**

Document exact non-secret defaults:

```dotenv
PAPER_LIBRARY_UPLOAD_MAX_MB=256
PAPER_LIBRARY_ENABLE_REMOTE_MANAGEMENT=0
```

- [ ] **Step 4: Document the browser workflow and recovery behavior**

Add README sections for upload fields, parse-only versus automatic translation, progress states, retry/cancel behavior, runtime file locations, upload cleanup, local-only protection, configuration, and terminal equivalents. State that existing papers are not rewritten by the feature.

- [ ] **Step 5: Run configuration and command suites**

Run: `conda run -n paper pytest -q tests/unit/test_config.py tests/scripts/test_commands.py`

Expected: PASS.

---

### Task 8: Full Verification Against an Isolated Library

**Files:**
- Create: `tests/integration/test_web_ingest_pipeline.py`
- Modify: `pyproject.toml` only if a test dependency used by the implemented tests is missing

**Interfaces:**
- Consumes: all production interfaces from Tasks 1–7.
- Produces: end-to-end evidence without modifying the real six-paper library.

- [ ] **Step 1: Build an isolated two-library integration fixture**

Copy only `library.toml` and normalized empty catalog headers into a temporary `libraries/` root. Use a fake MinerU executable that writes deterministic Markdown and one image, and an `httpx.MockTransport` translation response. Configure cache, state, and libraries entirely beneath `tmp_path`.

- [ ] **Step 2: Exercise the complete automatic workflow**

Upload `%PDF-fixture`, claim/run the job, and assert:

```python
assert final.state is JobState.COMPLETED
assert final.paper_id == "ws-p0001"
assert PaperPaths.for_id(whitespace, "ws-p0001").markdown.is_file()
assert translation_service.cached_many("whitespace/ws-p0001", version, segments)
assert client.get("/library/whitespace/paper/ws-p0001").status_code == 200
```

- [ ] **Step 3: Exercise restart recovery at every durable boundary**

Parameterize queued, parsing-before-publication, published-before-translation, and translating states. Recreate repository/workflow instances and assert one paper ID, valid catalog, no duplicate cache work, and a terminal job state.

- [ ] **Step 4: Run the full Python suite**

Run: `conda run -n paper pytest -q`

Expected: all tests pass with isolated `XDG_CACHE_HOME`, state directory, and model settings.

- [ ] **Step 5: Run shell, catalog, translation, package, and browser verification**

Run:

```bash
bash -n scripts/_common scripts/dev scripts/library scripts/translate
scripts/library check
scripts/translate check
scripts/translate status --all
scripts/dev doctor
conda run -n paper python -m pip wheel . --no-deps -w /tmp/paper-library-web-wheel
PAPER_TEST_BROWSER=<chromium> npm run test:browser
PAPER_TEST_BROWSER=<chromium> npm run test:management
```

Expected: two real libraries and six real papers remain valid; 1283 current translations remain cached; both browser suites pass; the wheel includes templates, CSS, JavaScript, and job modules.

- [ ] **Step 6: Start the real reader and perform read-only HTTP checks**

Run `scripts/dev restart`, require HTTP 200 from `/health/ready`, `/`, `/manage/import`, every existing paper/PDF/translation endpoint, and require the management form to list both libraries. Do not upload into the real libraries.

- [ ] **Step 7: Audit runtime and staged content**

Run `git diff --check` excluding preserved MinerU Markdown hard-break whitespace. Confirm `.env`, `.paper-library-dev`, uploaded PDFs, job databases, caches, tokens, API keys, and absolute personal paths are absent from staged files. Confirm `git diff --name-only` has no unstaged Paper Library edits after staging the authorized scope.
