"""Flask application for browsing normalized local paper libraries."""

from __future__ import annotations

from paper_library.metadata.citations import build_citations

import atexit
import hashlib
from functools import partial
from paper_library.ingest.service import ingest_pdf
from pathlib import Path
from urllib.parse import unquote

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
from paper_library.web.presentation import catalog_view, reading_navigation
from paper_library.web.images import image_dimensions

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
    from paper_library.jobs.security import _is_loopback
    if _is_loopback(settings.host):
        app.config['TRUSTED_HOSTS']=['localhost','127.0.0.1','[::1]',settings.host]
    from paper_library.ingest.service import _lock
    from paper_library.storage.locking import gate
    with gate(settings.paths.root):
        for root in settings.paths.libraries.glob('*'):
            if root.is_dir() and not root.is_symlink() and (root / '.transactions').exists():
                try:
                    with _lock(root):pass
                except (ValueError,OSError):
                    app.logger.exception('Publication recovery pending for %s',root.name)
    libraries = LibraryRepository(settings.paths.libraries)
    app.extensions["library_repository"] = libraries
    from paper_library.preferences import RuntimePreferences, ReadingTranslations
    preferences = RuntimePreferences(settings)
    app.extensions['runtime_preferences'] = preferences
    translation_service = ReadingTranslations(preferences)
    app.extensions["translation_service"] = translation_service
    jobs = JobRepository(settings.jobs_database,max_pending=int(settings.values.get("PAPER_JOB_MAX_PENDING","32")))
    jobs.reconcile_metadata_notices(libraries)
    jobs.reconcile_quality_notices()
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
    def workflow_factory():
        snapshot = preferences.snapshot()
        config = TranslationConfig.from_settings(snapshot.translation(required=False))
        translator = TranslationService(config, snapshot.paths.cache, max_concurrency=config.concurrency)
        from paper_library.ingest.service import refresh_metadata
        workflow = JobWorkflow(jobs, uploads, libraries, translator,
                            project_root=settings.paths.root,
                            ingest=partial(ingest_pdf, repair_settings=snapshot),
                            guide_generator=lambda path, **kwargs: GuideService(snapshot).generate(path, **kwargs),
                            structure_generator=lambda path, **kwargs: StructureService(snapshot).generate(path, **kwargs),
                            paragraph_repairer=partial(repair_file, settings=snapshot),
                            metadata_refresher=partial(refresh_metadata, settings=snapshot))
        workflow.configuration = {'revision': snapshot.revision, 'model': config.model}
        return workflow

    runner = job_runner or JobRunner(
        jobs, workflow_factory,
        uploads_directory=settings.uploads_directory,
        max_recoveries=int(settings.values.get("PAPER_JOB_MAX_RECOVERIES", "3")),
    )
    app.extensions.update(
        job_repository=jobs,
        job_runner=runner,
        upload_stager=uploads,
        management_guard=guard,
    )
    from paper_library.operations.settings_routes import settings_blueprint
    app.register_blueprint(settings_blueprint(preferences, guard, libraries))

    @app.url_value_preprocessor
    def select_library(_endpoint, values):
        g.library_id = values.pop("library_id", None) if values else None

    @app.url_defaults
    def library_url_defaults(endpoint, values):
        if endpoint in {"library_index", "paper_detail", "paper_pdf", "paper_asset", "paper_translation", "paper_guide_section"} and getattr(g, "library_id", None):
            values.setdefault("library_id", g.library_id)

    @app.context_processor
    def library_context():
        name = "论文库"
        if getattr(g, "library_id", None):
            try:
                name = _repository().load(g.library_id).descriptor.config.name
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
        from paper_library.operations.health import reading_status
        report = reading_status(current_app.config['PAPER_LIBRARY_SETTINGS'], repository=_repository())
        if not report['reading_ready']:
            return jsonify(status='error', checks=report['checks']), 503
        return jsonify(status='ok', libraries=len(_repository().scan()[0]))

    @app.get("/")
    def home():
        try:
            descriptors, issues = _repository().scan()
        except LibraryError:
            current_app.logger.exception("Unable to discover paper libraries")
            abort(503)
        libraries = []
        for library_id, descriptor in descriptors.items():
            try:
                snapshot = _repository().load(library_id)
                count = len(snapshot.papers)
                warning = "；".join(snapshot.warnings)
                if any(snapshot.descriptor.root.glob('.transactions/*/journal.json')):
                    warning = '存在未恢复的发布事务，请在运行状态页检查'
            except LibraryError:
                count = None
                warning = "论文目录暂时不可用"
            libraries.append(dict(id=library_id, name=descriptor.config.name, count=count, warning=warning))
        return render_template("libraries.html", libraries=libraries, discovery_issues=issues)

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
        return render_template("library.html", **catalog_view(snapshot, request.args), warnings=snapshot.warnings)

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
        navigation = dict(groups=[], main=None, combined=False)
        rendered: RenderedMarkdown | None = None
        if not paths.markdown.is_file():
            warnings.append("Markdown 文件不存在。")
        else:
            try:
                source = paths.markdown.read_text(encoding="utf-8")
                asset_prefix = f"{url_for('paper_detail', library_id=library_id, paper_id=paper.id)}/asset"
                structure = load_structure(paths.root, source)
                def image_size(url):
                    if not url.startswith(asset_prefix + '/'):
                        return None
                    try:
                        asset = resolve_paper_asset(root, paper, unquote(url[len(asset_prefix) + 1:]))
                        return image_dimensions(asset)
                    except (LibraryError, ValueError, OSError):
                        return None
                raw = render_markdown(source, asset_prefix, translation_slots=True,
                                      annotations=structure['annotations'] if structure else None, image_size=image_size)
                document_version = hashlib.sha256(source.encode()).hexdigest()
                rendered = RenderedMarkdown(Markup(raw.html), raw.headings, raw.warnings, raw.segments, raw.physical_headings)
                navigation = reading_navigation(source, raw.headings, raw.physical_headings)
                warnings.extend(raw.warnings)
            except (OSError, UnicodeError):
                warnings.append("Markdown 文件无法读取。")
            except Exception:
                current_app.logger.exception("Markdown rendering failed for %s", paper.id)
                warnings.append("Markdown 渲染失败，请查看终端日志。")
        guide = load_guide(paths.root, document_version) if rendered else None
        return render_template("paper.html", paper=paper, metadata=metadata, citations=build_citations(paper, metadata), rendered=rendered, document_version=document_version, pdf_available=pdf_available, warnings=tuple(warnings), guide=guide, navigation=navigation)

    @app.get("/library/<library_id>/paper/<paper_id>/guide")
    def paper_guide_section(paper_id: str):
        snapshot = _load_snapshot()
        paper = snapshot.find(paper_id)
        if paper is None or paper.id != paper_id:
            abort(404)
        paths = PaperPaths.for_id(snapshot.descriptor.root, paper.id)
        try:
            version = hashlib.sha256(paths.markdown.read_bytes()).hexdigest()
        except OSError:
            abort(404)
        if request.args.get('version') != version:
            return jsonify(error='论文正文已更新，请刷新页面。'), 409
        guide = load_guide(paths.root, version)
        if not guide or guide.get('stale'):
            return jsonify(error='章节导读暂不可用，请在导入任务中检查生成结果。'), 409
        section = next((row for row in guide['sections'] if row['id'] == request.args.get('section')), None)
        if section is None:
            abort(404)
        response = jsonify(document_version=version, html=render_template('guide-section.html', section=section))
        response.headers['Cache-Control'] = 'no-store'
        return response

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
        snapshot = _repository().load(g.library_id)
        if any(snapshot.descriptor.root.glob('.transactions/*/journal.json')):
            abort(503)
        return snapshot
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
