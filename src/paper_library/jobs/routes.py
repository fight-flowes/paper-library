"""Browser management routes for durable paper jobs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from flask import Blueprint, abort, jsonify, redirect, render_template, request, url_for

from paper_library.catalog import LibraryError, LibraryRepository
from paper_library.catalog.subjects import add_subject, list_subjects

from .models import InvalidTransition, JobRecord, JobState, NewJob
from .repository import JobRepository
from .runner import JobRunner
from .security import ManagementGuard
from .uploads import UploadError, UploadRequest, UploadStager


@dataclass(frozen=True, slots=True)
class JobDependencies:
    jobs: JobRepository
    uploads: UploadStager
    guard: ManagementGuard
    runner: JobRunner
    libraries: LibraryRepository
    upload_max_bytes: int


def create_management_blueprint(dependencies: JobDependencies) -> Blueprint:
    blueprint = Blueprint("management", __name__)

    @blueprint.after_request
    def no_store(response):
        response.headers["Cache-Control"] = "no-store"
        return response

    @blueprint.get("/manage/import")
    def import_page():
        return _render_import(dependencies)

    @blueprint.post("/manage/jobs")
    def create_job():
        _authorize(dependencies)
        uploaded = request.files.get("pdf")
        if uploaded is None:
            return _render_import(dependencies, error="请选择 PDF 文件", status=400)
        upload_request = UploadRequest(
            library_id=request.form.get("library_id", ""),
            subject=request.form.get("subject", ""),
            language=request.form.get("language", "en"),
            enrich=request.form.get("enrich") == "1",
            auto_translate=request.form.get("auto_translate") == "1",
        )
        staged = None
        try:
            staged = dependencies.uploads.stage(uploaded, upload_request)
            job = dependencies.jobs.create(
                NewJob(
                    original_filename=staged.original_filename,
                    library_id=staged.request.library_id,
                    subject=staged.request.subject,
                    language=staged.request.language,
                    enrich=staged.request.enrich,
                    auto_translate=staged.request.auto_translate,
                    upload_name=staged.upload_name,
                )
            )
        except (UploadError, LibraryError, OSError, ValueError) as error:
            if staged is not None:
                dependencies.uploads.remove(staged.upload_name)
            return _render_import(dependencies, error=str(error), status=400)
        dependencies.runner.wake()
        return redirect(url_for("management.job_detail", job_id=job.id), code=303)

    @blueprint.post('/manage/subjects')
    def create_subject():
        _authorize(dependencies)
        try:
            name = add_subject(dependencies.libraries, request.form.get('library_id', ''),
                               request.form.get('subject', ''))
        except (LibraryError, ValueError, OSError) as error:
            return jsonify(error=str(error)), 400
        return jsonify(subject=name), 201

    @blueprint.get("/manage/jobs/<job_id>")
    def job_detail(job_id: str):
        job = dependencies.jobs.get(job_id)
        if job is None:
            abort(404)
        return render_template(
            "job.html",
            job=job,
            management_token=dependencies.guard.token,
            paper_url=_paper_url(job),
            mutations_enabled=dependencies.guard.mutations_enabled,
        )

    @blueprint.get("/api/jobs/<job_id>")
    def job_status(job_id: str):
        job = dependencies.jobs.get(job_id)
        if job is None:
            abort(404)
        return jsonify(_serialize(job))

    @blueprint.get("/api/jobs")
    def recent_jobs():
        return jsonify(jobs=[_serialize(job) for job in dependencies.jobs.recent(limit=20)])

    @blueprint.post("/manage/jobs/<job_id>/retry")
    def retry_job(job_id: str):
        _authorize(dependencies)
        job = dependencies.jobs.get(job_id)
        if job is None:
            abort(404)
        if job.state not in {JobState.FAILED, JobState.COMPLETED_WITH_WARNINGS}:
            abort(409)
        dependencies.jobs.retry(job_id)
        dependencies.runner.wake()
        return redirect(url_for("management.job_detail", job_id=job_id), code=303)

    @blueprint.post("/manage/jobs/<job_id>/translate")
    def translate_job(job_id: str):
        _authorize(dependencies)
        if dependencies.jobs.get(job_id) is None:
            abort(404)
        try:
            dependencies.jobs.enable_translation(job_id)
        except InvalidTransition:
            abort(409)
        dependencies.runner.wake()
        return redirect(url_for("management.job_detail", job_id=job_id), code=303)

    @blueprint.post("/manage/jobs/<job_id>/cancel")
    def cancel_job(job_id: str):
        _authorize(dependencies)
        job = dependencies.jobs.get(job_id)
        if job is None:
            abort(404)
        if job.state in {JobState.QUEUED, JobState.RUNNING}:
            cancelled = dependencies.runner.cancel(job_id)
            if cancelled.state is JobState.CANCELLED:
                dependencies.uploads.remove(cancelled.upload_name)
        return redirect(url_for("management.job_detail", job_id=job_id), code=303)

    return blueprint


def _authorize(dependencies: JobDependencies) -> None:
    if not dependencies.guard.mutations_enabled:
        abort(403)
    if not dependencies.guard.validate(request.form.get("management_token")):
        abort(403)


def _render_import(
    dependencies: JobDependencies,
    *,
    error: str | None = None,
    status: int = 200,
):
    try:
        libraries = dependencies.libraries.discover()
        subjects_by_library = {
            library_id: list_subjects(dependencies.libraries, library_id)
            for library_id in libraries
        }
        subjects = sorted(
            {subject for values in subjects_by_library.values() for subject in values},
            key=str.casefold,
        )
    except (LibraryError, ValueError, OSError):
        libraries = {}
        subjects = []
        subjects_by_library = {}
        error = error or "论文库目录暂时不可用"
        status = 503
    return (
        render_template(
            "import.html",
            libraries=libraries,
            subjects=subjects,
            subjects_by_library=subjects_by_library,
            jobs=dependencies.jobs.recent(limit=20),
            management_token=dependencies.guard.token,
            mutations_enabled=dependencies.guard.mutations_enabled,
            upload_max_mb=dependencies.upload_max_bytes // (1024 * 1024),
            error=error,
        ),
        status,
    )


def _paper_url(job: JobRecord) -> str | None:
    if not job.paper_id:
        return None
    return url_for("paper_detail", library_id=job.library_id, paper_id=job.paper_id)


def _serialize(job: JobRecord) -> dict[str, Any]:
    return {
        "id": job.id,
        "filename": job.original_filename,
        "library": job.library_id,
        "subject": job.subject,
        "state": job.state.value,
        "stage": job.stage.value,
        "paper_url": _paper_url(job),
        "progress": {
            "total": job.progress.total,
            "cached": job.progress.cached,
            "translated": job.progress.translated,
            "failed": job.progress.failed,
            "pending": job.progress.pending,
        },
        "activity": job.activity,
        "outcomes": job.outcomes,
        "error": job.error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }
