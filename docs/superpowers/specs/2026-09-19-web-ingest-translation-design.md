# Web PDF Ingest and Translation Design

## Goal

Add a local management flow to Paper Library so a user can select a PDF in the browser, choose an existing library and subject, run MinerU ingestion, optionally translate the parsed paper, monitor progress, and open the resulting paper without using terminal commands.

The browser remains a client of the existing modular monolith. PDF parsing, catalog publication, translation, and filesystem access continue to run on the local machine.

## User Flow

The libraries home page gains an **导入论文** action. It opens `/manage/import`, which contains:

- a local PDF file picker;
- a required target library selector populated from `LibraryRepository.discover()`;
- a subject input with suggestions from the selected library's existing catalog and support for a new subject;
- a document-language selector (`en` or `ch`);
- an **解析后自动翻译** checkbox, enabled by default;
- an option to skip bibliographic enrichment when the local metadata gateway is unavailable or undesired.

Submitting the form uploads the PDF into a project-local runtime staging directory and creates a persistent background job. The response redirects immediately to the job page rather than holding the HTTP request open.

The job page displays these stages:

1. 已排队
2. 正在解析 PDF
3. 正在整理元数据并发布论文
4. 正在翻译段落, including completed, cached, failed, and total segment counts
5. 已完成, with a link to the new paper

The page polls a read-only JSON status endpoint. It shows a concise sanitized failure message and a retry action when a job fails. A queued job may be cancelled. Translation cancellation is cooperative: no new segments are submitted, and in-flight model requests finish and save before the job becomes cancelled. MinerU cancellation terminates only the subprocess owned by that job.

## Architecture

This remains a modular monolith. No Redis, Celery, separate daemon, or new service is introduced.

New modules under `src/paper_library/jobs/` own the background workflow:

- `models.py`: immutable job and progress records plus state-transition validation;
- `repository.py`: SQLite persistence and atomic job claiming;
- `runner.py`: one background coordinator and cancellation registry;
- `workflow.py`: orchestration across ingestion and translation;
- `routes.py`: management HTML and JSON endpoints.

The Flask application creates one `JobRunner` extension. The runner uses one coordinator thread so only one PDF is parsed at a time. Translation for the active job continues to use `run_batch` and the selected model's configured concurrency, currently DeepSeek with 16 workers. This avoids concurrent MinerU jobs competing for local compute while preserving existing translation throughput.

The runner starts only for the real application process. Tests may inject a synchronous or disabled runner. Starting a second reader process cannot execute the same job because claiming uses an SQLite transaction and a runner lease.

## Persistent Runtime State

Runtime files live beneath the ignored `.paper-library-dev/` directory:

```text
.paper-library-dev/
├── jobs.sqlite3
└── uploads/
    └── <job-id>/
        └── source.pdf
```

The jobs database stores:

- job ID and timestamps;
- original filename;
- target library ID and subject;
- language, enrichment choice, and automatic-translation choice;
- state and current stage;
- paper ID after publication;
- translation totals and counts;
- sanitized error summary;
- cancellation flag;
- runner lease and heartbeat.

It never stores model credentials, request payloads, full PDF text, or translation text. Translation text remains in the existing translation cache.

On startup, an expired active lease is reclaimed. The workflow is idempotent:

- an unpublished upload is parsed again from its staged PDF;
- a PDF already published with the same SHA-256 resolves to the existing paper;
- translation resumes from the segment cache;
- a completed job is never run again unless the user explicitly creates or retries a job.

Uploaded PDFs are retained while a job is active or failed. They are deleted after successful publication or explicit cancellation. Retry uses the retained upload. A periodic cleanup removes cancelled and completed staging directories left by an abnormal shutdown.

## Ingestion Changes

`paper_library.ingest.service` gains a structured result rather than returning a display string. The result includes the library ID, stable paper ID, outcome (`created`, `updated`, or `unchanged`), and final paths.

The ingestion API accepts injected callbacks for stage updates and an injected MinerU runner. MinerU is executed with `subprocess.Popen` in its own process group so the job runner can stream bounded progress and terminate the correct process. Terminal commands continue to use the same service.

Publication keeps the existing library lock and fixed package layout. Catalog replacement and package replacement are treated as one recoverable transaction: the previous package and catalog are restored if either publication step fails. The job is not marked published until repository validation can load the resulting paper.

Subjects are catalog labels, not filesystem paths. They are normalized to a single line, trimmed, and limited to 120 Unicode characters. Library IDs can only come from discovery; the client cannot submit arbitrary filesystem locations.

## Translation Changes

When automatic translation is enabled, the workflow loads tasks for the newly published `library-id/paper-id` and calls the existing batch translator. Its `on_update` callback persists aggregate counters at a throttled rate so 16 concurrent workers do not produce excessive SQLite writes.

Translation keeps the existing behavior:

- model and concurrency come from `.env`;
- successful segments are saved immediately;
- cached segments are skipped;
- lenient validation warnings are preserved;
- a few failed segments do not remove the parsed paper.

A job with translation failures finishes as `completed_with_warnings`. The paper link remains available and the UI reports the number of failed segments. Retrying resumes only missing segments. A model configuration or connectivity error moves the job to `failed` with the parsed paper ID retained, allowing translation retry without rerunning MinerU.

When automatic translation is disabled, the job completes after publication. Its page presents a **开始翻译** action that creates the translation stage for the same job.

## HTTP Interface

HTML routes:

- `GET /manage/import` — upload form and recent jobs;
- `GET /manage/jobs/<job-id>` — job progress and result page.

Mutation routes:

- `POST /manage/jobs` — validate and stage one PDF;
- `POST /manage/jobs/<job-id>/translate` — start or resume translation;
- `POST /manage/jobs/<job-id>/retry` — retry the failed stage;
- `POST /manage/jobs/<job-id>/cancel` — request cancellation.

Read-only JSON routes:

- `GET /api/jobs` — recent jobs;
- `GET /api/jobs/<job-id>` — one job's status and progress.

Mutations use ordinary HTML forms with POST/Redirect/GET. A random management token is generated in the runtime state directory and embedded as a hidden form field. The server compares it with `secrets.compare_digest`; a cross-origin page cannot read the token. JSON status endpoints expose no filesystem paths or credentials and return `Cache-Control: no-store`.

## Validation and Local Security

The feature remains loopback-only under the existing default host. It additionally enforces:

- a configurable `PAPER_LIBRARY_UPLOAD_MAX_MB`, defaulting to 256 MiB;
- one file per job;
- `.pdf` extension and `%PDF-` signature validation;
- generated job directories rather than user-controlled paths;
- no use of the browser filename as a destination path;
- rejection of unknown libraries, invalid subjects, and unsupported languages;
- bounded job/event history and sanitized subprocess/model errors;
- ownership checks before terminating MinerU;
- no API endpoint for reading arbitrary uploaded files.

The development server is intended for local use. If the host is configured as a non-loopback address, management mutations are disabled unless `PAPER_LIBRARY_ENABLE_REMOTE_MANAGEMENT=1` is explicitly set.

## UI Behavior

The management pages reuse the existing typography, spacing, and color tokens. The form uses a compact two-column layout on desktop and one column on mobile. Library selection uses the human-readable name with the stable ID shown as secondary text. Subject suggestions update when the library changes but the field remains editable.

Progress uses a stage list and one dynamic progress bar. It does not append one line per translated segment. Polling backs off after completion or failure. The UI works without JavaScript for upload, retry, cancel, and navigation; JavaScript adds live updates and subject suggestions.

## Error Handling

Validation errors return the form with the user's safe selections preserved. Once a job exists, failures are recorded on the job rather than returned as long-running HTTP errors.

Expected failures are mapped to clear messages:

- invalid or oversized PDF;
- target library changed or disappeared;
- another publication holds the library lock;
- MinerU missing, terminated, or returning invalid output;
- metadata gateway unavailable, which is non-fatal;
- translation model missing or unavailable;
- cache or runtime-state write failure.

Application logs retain tracebacks for diagnosis. Browser responses contain no credentials, absolute paths, raw model response bodies, or uncontrolled subprocess output.

## Testing

Unit tests cover:

- job state transitions and invalid transitions;
- SQLite claiming, leases, progress updates, cancellation, and recovery;
- file size, signature, library, subject, and language validation;
- structured ingestion outcomes and rollback;
- workflow behavior for parse-only, parse-and-translate, retry, duplicate PDF, partial translation failure, and cancellation.

Integration tests use a fake MinerU runner and mock translation transport. They verify upload through the Flask client, catalog publication, job persistence across app recreation, stable paper URLs, cached translation resumption, CSRF rejection, and absence of secrets and filesystem paths in JSON.

Playwright verifies desktop and mobile upload forms, library and subject selection, live stage updates, completed-paper navigation, parse-only translation action, failure retry, and that progress remains a single dynamic display.

Real-project verification uses a temporary copied library and a small fixture PDF. It does not modify the six existing papers. Existing reader, catalog, translation, command, package-build, and browser tests remain required.

## Success Criteria

- A local PDF can be submitted entirely from the browser.
- The user can choose Whitespace or Mechanism Interpretability and an existing or new subject.
- The request returns promptly while parsing and translation continue in the background.
- Refreshing or restarting the reader does not lose job state or duplicate a published paper.
- Automatic translation is selectable and enabled by default.
- Translation progress is compact and uses the configured DeepSeek concurrency.
- On success, the new paper appears in the selected library and opens from the job page.
- Failures are actionable and retryable without corrupting catalogs or existing papers.
- Existing terminal commands and read-only paper pages continue to work.
