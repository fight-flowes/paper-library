"""Flask application for browsing normalized local paper libraries."""

from __future__ import annotations

import atexit
import hashlib
from functools import partial
from paper_library.ingest.service import ingest_pdf
from pathlib import Path

from flask import Flask, request, abort, current_app, g, jsonify, redirect, render_template, send_file, url_for
from markupsafe import Markup

from paper_library.catalog import (
    LibraryError, LibraryRepository, LibrarySnapshot, PaperPaths,
    read_metadata, resolve_paper_asset,
)
from paper_library.config import Settings, default_settings
from paper_library.reading.service import GuideService
from paper_library.ingest.paragraphs import repair_file
from paper_library.jobs import (
    JobRepository,
    JobRunner,
    JobWorkflow,
    ManagementGuard,
    UploadStager,
)
from paper_library.jobs.routes import JobDependencies, create_management_blueprint
from paper_library.translation.routes import register_translation_routes
from paper_library.translation.service import TranslationConfig, TranslationService
from paper_library.web.markdown import RenderedMarkdown, render_translatable_markdown, render_markdown
from paper_library.structure.document import load_structure
from paper_library.structure.service import StructureService
from paper_library.reading.service import load_guide

DEFAULT_LIBRARY_ID = "mechanism-interpretability"


def create_app(
    settings: Settings | None = None,
    *,
    job_runner: JobRunner | None = None,
    start_jobs: bool = True,
) -> Flask:
    """Create the reader using the project's single configuration source."""
    settings = settings or default_settings()
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.update(
        PAPER_LIBRARY_SETTINGS=settings,
        SEND_FILE_MAX_AGE_DEFAULT=0,
        MAX_CONTENT_LENGTH=settings.upload_max_bytes + 1024 * 1024,
    )
    from paper_library.ingest.service import _lock
    for root in settings.paths.libraries.glob('*'):
        if root.is_dir() and (root / '.transactions').exists():
            try:
                with _lock(root):pass
            except (ValueError,OSError):
                app.logger.exception('Publication recovery pending for %s',root.name)
    libraries = LibraryRepository(settings.paths.libraries)
    app.extensions["library_repository"] = libraries
    translation = TranslationConfig.from_settings(settings.translation(required=False))
    translation_service = TranslationService(
        translation, settings.paths.cache, max_concurrency=translation.concurrency
    )
    app.extensions["translation_service"] = translation_service
    jobs = JobRepository(settings.jobs_database,max_pending=int(settings.values.get("PAPER_JOB_MAX_PENDING","32")))
    uploads = UploadStager(
        settings.uploads_directory,
        max_bytes=settings.upload_max_bytes,
        reserve_bytes=int(settings.values.get("PAPER_MIN_FREE_MB", "1024"))*1024*1024,
        libraries=libraries,
    )
    guard = ManagementGuard.from_state_directory(
        settings.paths.state,
        settings.host,
        settings.remote_management_enabled,
    )
    runner = job_runner or JobRunner(
        jobs,
        lambda: JobWorkflow(jobs, uploads, libraries, translation_service,
                            project_root=settings.paths.root,
                            ingest=partial(ingest_pdf, repair_settings=settings),
                            guide_generator=lambda path, **kwargs: GuideService(settings).generate(path, **kwargs),
                            structure_generator=lambda path, **kwargs: StructureService(settings).generate(path, **kwargs),
                            paragraph_repairer=partial(repair_file, settings=settings)),
        uploads_directory=settings.uploads_directory,
        max_recoveries=int(settings.values.get("PAPER_JOB_MAX_RECOVERIES", "3")),
    )
    app.extensions.update(
        job_repository=jobs,
        job_runner=runner,
        upload_stager=uploads,
        management_guard=guard,
    )

    @app.url_value_preprocessor
    def select_library(_endpoint, values):
        g.library_id = values.pop("library_id", None) if values else None

    @app.url_defaults
    def library_url_defaults(endpoint, values):
        if endpoint in {"library_index", "paper_detail", "paper_pdf", "paper_asset", "paper_translation"} and getattr(g, "library_id", None):
            values.setdefault("library_id", g.library_id)

    @app.context_processor
    def library_context():
        name = "论文库"
        if getattr(g, "library_id", None):
            try:
                name = _repository().discover()[g.library_id].config.name
            except (KeyError, LibraryError):
                pass
        return {"library_name": name, "multi_library": True}

    from paper_library.personal.store import PersonalStore
    from paper_library.personal.search import SearchIndex
    from paper_library.personal.routes import personal_blueprint
    from paper_library.storage.locking import gate
    personal = PersonalStore(settings.paths.state / 'personal.sqlite3')
    search = SearchIndex(settings.paths.state / 'search.sqlite3')
    app.extensions['personal_store'] = personal
    app.extensions['search_index'] = search
    app.register_blueprint(personal_blueprint(libraries,personal,search,translation_service,guard))

    @app.before_request
    def protect_writes():
        if request.method in {'POST','PUT','PATCH','DELETE'}:
            context=gate(settings.paths.root,blocking=False)
            try:context.__enter__()
            except ValueError:return jsonify(error='正在执行备份维护，请稍后重试'),503
            g.write_gate=context

    @app.teardown_request
    def release_write_gate(error):
        context=g.pop('write_gate',None)
        if context:context.__exit__(None,None,None)

    @app.context_processor
    def personal_context():
        return {'personal_token':guard.token if guard.mutations_enabled else '', 'personal_enabled':guard.mutations_enabled}

    _register_routes(app)
    register_translation_routes(app, _load_snapshot, _library_root, lambda: g.library_id)
    app.register_blueprint(
        create_management_blueprint(
            JobDependencies(
                jobs,
                uploads,
                guard,
                runner,
                libraries,
                settings.upload_max_bytes,
            )
        )
    )
    from paper_library.operations.routes import operations_blueprint
    app.register_blueprint(operations_blueprint(settings,jobs))
    _register_error_handlers(app)
    if start_jobs:
        search.start(libraries,translation_service)
        atexit.register(search.stop)
        runner.start()
        atexit.register(runner.stop)
    return app


def _register_routes(app: Flask) -> None:
    @app.get("/health/live")
    def health_live():
        return jsonify(status="ok")

    @app.get("/health/ready")
    def health_ready():
        try:
            descriptors = _repository().discover()
            for library_id in descriptors:
                _repository().load(library_id)
        except LibraryError as error:
            return jsonify(status="error", error=str(error)), 503
        import sqlite3
        try:
            current_app.extensions['job_repository'].state_counts()
            with current_app.extensions['personal_store'].connect() as database:
                database.execute('SELECT 1 FROM notes LIMIT 1')
        except sqlite3.Error:
            return jsonify(status='error',error='运行数据库不可用'),503
        return jsonify(status="ok", libraries=len(descriptors))

    @app.get("/")
    def home():
        try:
            descriptors = _repository().discover()
        except LibraryError:
            current_app.logger.exception("Unable to discover paper libraries")
            abort(503)
        libraries = []
        for library_id, descriptor in descriptors.items():
            try:
                snapshot = _repository().load(library_id)
                count = len(snapshot.papers)
                warning = "；".join(snapshot.warnings)
            except LibraryError:
                count = None
                warning = "论文目录暂时不可用"
            libraries.append(dict(id=library_id, name=descriptor.config.name, count=count, warning=warning))
        return render_template("libraries.html", libraries=libraries)

    @app.get("/paper/<paper_id>")
    def legacy_paper_detail(paper_id: str):
        return redirect(url_for("paper_detail", library_id=DEFAULT_LIBRARY_ID, paper_id=paper_id), code=308)

    @app.get("/paper/<paper_id>/pdf")
    def legacy_paper_pdf(paper_id: str):
        return redirect(url_for("paper_pdf", library_id=DEFAULT_LIBRARY_ID, paper_id=paper_id), code=308)

    @app.get("/paper/<paper_id>/asset/<path:asset_path>")
    def legacy_paper_asset(paper_id: str, asset_path: str):
        return redirect(url_for("paper_asset", library_id=DEFAULT_LIBRARY_ID, paper_id=paper_id, asset_path=asset_path), code=308)

    @app.get("/paper/<paper_id>/translation")
    def legacy_paper_translation(paper_id: str):
        return redirect(url_for("paper_translation", library_id=DEFAULT_LIBRARY_ID, paper_id=paper_id), code=308)

    @app.get("/library/<library_id>/")
    def library_index():
        snapshot = _load_snapshot()
        return render_template("library.html", groups=snapshot.grouped(), paper_count=len(snapshot.papers), warnings=snapshot.warnings)

    @app.get("/library/<library_id>/paper/<paper_id>")
    def paper_detail(paper_id: str):
        library_id = g.library_id
        snapshot = _load_snapshot()
        paper = snapshot.find(paper_id)
        if paper is None:
            abort(404)
        if paper_id != paper.id:
            return redirect(url_for("paper_detail", library_id=library_id, paper_id=paper.id), code=308)
        root = snapshot.descriptor.root
        metadata, metadata_warnings = read_metadata(root, paper)
        warnings = list(snapshot.warnings) + list(metadata_warnings)
        paths = PaperPaths.for_id(root, paper.id)
        pdf_available = paths.pdf.is_file()
        if not pdf_available:
            warnings.append("PDF 文件不存在。")
        document_version = ""
        rendered: RenderedMarkdown | None = None
        if not paths.markdown.is_file():
            warnings.append("Markdown 文件不存在。")
        else:
            try:
                source = paths.markdown.read_text(encoding="utf-8")
                asset_prefix = f"{url_for('paper_detail', library_id=library_id, paper_id=paper.id)}/asset"
                structure = load_structure(paths.root, source)
                raw = (render_markdown(source, asset_prefix, translation_slots=True, annotations=structure['annotations'])
                       if structure else render_translatable_markdown(source, asset_prefix))
                document_version = hashlib.sha256(source.encode()).hexdigest()
                rendered = RenderedMarkdown(Markup(raw.html), raw.headings, raw.warnings, raw.segments)
                warnings.extend(raw.warnings)
            except (OSError, UnicodeError):
                warnings.append("Markdown 文件无法读取。")
            except Exception:
                current_app.logger.exception("Markdown rendering failed for %s", paper.id)
                warnings.append("Markdown 渲染失败，请查看终端日志。")
        guide = load_guide(paths.root, document_version) if rendered else None
        return render_template("paper.html", paper=paper, metadata=metadata, rendered=rendered, document_version=document_version, pdf_available=pdf_available, warnings=tuple(warnings), guide=guide)

    @app.get("/library/<library_id>/paper/<paper_id>/pdf")
    def paper_pdf(paper_id: str):
        library_id = g.library_id
        snapshot = _load_snapshot()
        paper = snapshot.find(paper_id)
        if paper is None:
            abort(404)
        if paper_id != paper.id:
            return redirect(url_for("paper_pdf", library_id=library_id, paper_id=paper.id), code=308)
        path = PaperPaths.for_id(snapshot.descriptor.root, paper.id).pdf
        if not path.is_file():
            abort(404)
        return send_file(path, mimetype="application/pdf", as_attachment=False, download_name=path.name, conditional=True)

    @app.get("/library/<library_id>/paper/<paper_id>/asset/<path:asset_path>")
    def paper_asset(paper_id: str, asset_path: str):
        library_id = g.library_id
        snapshot = _load_snapshot()
        paper = snapshot.find(paper_id)
        if paper is None:
            abort(404)
        if paper_id != paper.id:
            return redirect(url_for("paper_asset", library_id=library_id, paper_id=paper.id, asset_path=asset_path), code=308)
        try:
            path = resolve_paper_asset(snapshot.descriptor.root, paper, asset_path)
        except ValueError:
            abort(404)
        return send_file(path, conditional=True)


def _register_error_handlers(app: Flask) -> None:
    @app.errorhandler(404)
    def not_found(_error):
        return render_template("error.html", title="页面不存在", message="没有找到这篇论文或文件。"), 404

    @app.errorhandler(503)
    def unavailable(_error):
        return render_template("error.html", title="论文目录暂时不可用", message="请运行 scripts/library check 后刷新页面。"), 503


def _repository() -> LibraryRepository:
    return current_app.extensions["library_repository"]


def _load_snapshot() -> LibrarySnapshot:
    if not getattr(g, "library_id", None):
        abort(404)
    try:
        return _repository().load(g.library_id)
    except LibraryError:
        current_app.logger.exception("Unable to load paper library %s", g.library_id)
        abort(503)


def _library_root() -> Path:
    return _load_snapshot().descriptor.root


def main() -> None:
    settings = default_settings()
    from paper_library.operations.logging import configure
    import signal
    settings.paths.state.mkdir(parents=True,exist_ok=True)
    configure(settings)
    app=create_app(settings)
    from waitress import create_server
    server=create_server(app,host=settings.host,port=settings.port,
                         threads=int(settings.values.get('PAPER_HTTP_THREADS','8')),
                         max_request_body_size=settings.upload_max_bytes+1024*1024,
                         clear_untrusted_proxy_headers=True)
    def shutdown(signum,frame):
        server.close()
        app.extensions['search_index'].stop()
        app.extensions['job_runner'].stop(timeout=30)
        raise SystemExit(0)
    signal.signal(signal.SIGTERM,shutdown)
    signal.signal(signal.SIGINT,shutdown)
    app.logger.info('reader_http_started')
    server.run()


if __name__ == "__main__":
    main()
