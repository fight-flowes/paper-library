"""Read-only access to translations prepared by the terminal command."""
from __future__ import annotations

import hashlib

from flask import current_app, jsonify, redirect, request, url_for

from paper_library.catalog import PaperPaths
from paper_library.web.markdown import render_translatable_markdown
from paper_library.translation.service import TranslationError


def register_translation_routes(app, load_snapshot, project_root, library_id):
    @app.route('/library/<library_id>/paper/<paper_id>/translation', methods=['GET'])
    def paper_translation(paper_id):
        try:
            paper = load_snapshot().find(paper_id)
            if paper is None:
                raise TranslationError('论文不存在。', 404)
            if paper_id != paper.id:
                return redirect(url_for('paper_translation', paper_id=paper.id), code=308)
            root = project_root()
            try:
                path = PaperPaths.for_id(root, paper.id).markdown
                source = path.read_text(encoding='utf-8')
            except (ValueError, OSError, UnicodeError) as error:
                raise TranslationError('论文正文暂时无法读取。', 404) from error
            version = hashlib.sha256(source.encode()).hexdigest()
            prefix = url_for('paper_detail', paper_id=paper.id) + '/asset'
            rendered = render_translatable_markdown(source, prefix)
            identity = f"{library_id()}/{paper.id}"
            service = current_app.extensions['translation_service']
            saved = service.cached_many(identity, version, rendered.segments)
            sources = {key: service.config.model for key in saved}
            segments = [
                {'id': segment.id,
                 'model': sources.get(segment.id),
                 'html': segment.render_translation(saved[segment.id]) if segment.id in saved else None}
                for segment in rendered.segments
            ]
            return jsonify(document_version=version, model=service.config.model,
                           translated=len(saved), total=len(segments), segments=segments)
        except TranslationError as error:
            return jsonify(error=str(error)), error.status

    @app.after_request
    def translation_no_cache(response):
        if request.endpoint == 'paper_translation':
            response.headers['Cache-Control'] = 'no-store'
        return response
