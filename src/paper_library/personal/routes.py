"""Local search and personal reading routes."""
import hashlib
import json
import re
import sqlite3
from markupsafe import Markup, escape
from urllib.parse import urlsplit

from flask import Blueprint,abort,jsonify,render_template,request,Response,url_for

from paper_library.catalog import PaperPaths,LibraryError
from paper_library.web.markdown import render_translatable_markdown
from paper_library.translation.service import TranslationError
from .text import TextMap,matching_positions,relocate
from .store import ProgressConflict


def personal_blueprint(libraries,store,index,translations,guard):
    bp=Blueprint('personal',__name__)

    def document(library,paper):
        try:
            snapshot=libraries.load(library);record=snapshot.find(paper)
            if record is None or record.id!=paper:abort(404)
            path=PaperPaths.for_id(snapshot.descriptor.root,paper).markdown
            source=path.read_text(encoding='utf-8')
        except (LibraryError,OSError,UnicodeError):abort(404)
        version=hashlib.sha256(source.encode()).hexdigest()
        return TextMap(render_translatable_markdown(source,'/asset').html).blocks,version

    def authorize():
        if not guard.mutations_enabled or not guard.validate(request.headers.get('X-Management-Token')):abort(403)
        origin=request.headers.get('Origin')
        if origin and urlsplit(origin).netloc!=request.host:abort(403)

    @bp.after_request
    def no_cache(response):
        response.headers['Cache-Control']='no-store'
        return response

    @bp.errorhandler(ProgressConflict)
    def conflict(exc):return jsonify(error=str(exc),conflict=True),409

    @bp.errorhandler(ValueError)
    def invalid(exc):return jsonify(error=str(exc)),400

    @bp.app_template_filter('highlight_query')
    def highlight_query(text, query):
        if not query.strip():return escape(text)
        parts=[];position=0
        for match in re.finditer(re.escape(query.strip()),text,re.IGNORECASE):
            parts.extend([escape(text[position:match.start()]),Markup('<mark>'),escape(match.group()),Markup('</mark>')]);position=match.end()
        parts.append(escape(text[position:]));return Markup('').join(parts)

    @bp.get('/search')
    def search():
        query=request.args.get('q','')[:200];errors=[];rows=[];total_papers=total_hits=0
        page=max(1,request.args.get('page',1,type=int) or 1)
        library_filter=request.args.get('library','')[:120]
        origin_filter=request.args.get('origin','')
        if origin_filter not in {'metadata','original','translation'}:origin_filter=''
        try: library_names={key:descriptor.config.name for key,descriptor in libraries.scan()[0].items()}
        except LibraryError: library_names={}
        if query.strip():
            try:
                errors=list(index.errors) if index.background else index.sync(libraries,translations)
                if index.background and not index.ready:errors.append('索引正在更新，请稍后刷新')
                rows,total_papers,total_hits=index.page(query,page=page,library=library_filter,origin=origin_filter)
            except (LibraryError,TranslationError,OSError,sqlite3.Error):errors=['检索索引暂不可用，已有论文仍可阅读']
        groups={}
        for row in rows:
            key=(row['library'],row['paper'])
            groups.setdefault(key,dict(library=row['library'],paper=row['paper'],title=row['title'],hits=[]))['hits'].append(row)
        return render_template('search.html',query=query,results=rows,groups=list(groups.values()),index_errors=errors,page=page,total_papers=total_papers,total_hits=total_hits,has_next=page*20<total_papers,
                               library_names=library_names,library_filter=library_filter,origin_filter=origin_filter)

    @bp.get('/reading')
    def shelf():
        rows=store.shelf();notes=store.list();catalog={}
        try:
            for library in libraries.scan()[0]:
                try:
                    for paper in libraries.load(library).papers:catalog[(library,paper.id)]=paper.title
                except LibraryError:continue
        except LibraryError:pass
        for row in rows:
            row['progress']=json.loads(row['value']);row['title']=catalog.get((row['library'],row['paper']),row['paper'])
            row['exists']=(row['library'],row['paper']) in catalog
        for row in notes:row['title']=catalog.get((row['library'],row['paper']),row['paper'])
        return render_template('reading.html',readings=rows,notes=notes)

    @bp.get('/reading/export')
    def export():
        notes=store.list();progress=store.shelf()
        if request.args.get('format')=='markdown':
            text='# 阅读标注\n\n'
            for row in notes:
                text+='## '+row['library']+'/'+row['paper']+'\n\n> '+row['quote'].replace('\n','\n> ')+'\n\n'+row['body']+'\n\n'
            return Response(text,mimetype='text/markdown',headers={'Content-Disposition':'attachment; filename="reading-notes.md"'})
        return Response(json.dumps({'schema':1,'notes':notes,'progress':progress},ensure_ascii=False,indent=2),mimetype='application/json',headers={'Content-Disposition':'attachment; filename="reading-data.json"'})

    @bp.route('/api/reading/<library_key>/<paper_key>',methods=['GET','POST'])
    def paper_records(library_key,paper_key):
        blocks,version=document(library_key,paper_key)
        if request.method=='POST':
            authorize();data=request.get_json(silent=True)
            if not isinstance(data,dict):raise ValueError('请求必须为 JSON 对象')
            if data.get('action')=='progress':
                revision=store.save_progress(library_key,paper_key,data,blocks,version)
                return jsonify(saved=True,revision=revision)
            key=store.create(library_key,paper_key,data,blocks,version)
            return jsonify(id=key),201
        notes=[relocate(row,blocks,version) for row in store.list(library_key,paper_key)]
        block_order={anchor:index for index,anchor in enumerate(blocks)}
        def document_position(note):
            anchor=note['resolved_anchor']
            if anchor not in block_order:
                return (len(block_order), float('inf'))
            positions=matching_positions(blocks[anchor],note['quote'],note['prefix'],note['suffix'])
            return (block_order[anchor],positions[0] if len(positions)==1 else float('inf'))
        notes.sort(key=document_position)
        progress=store.progress(library_key,paper_key)
        return jsonify(notes=notes,progress=relocate(progress,blocks,version) if progress else None,document_version=version)

    @bp.route('/api/reading/<library_key>/<paper_key>/<note_id>',methods=['PATCH','DELETE'])
    def edit_record(library_key,paper_key,note_id):
        authorize()
        if request.method=='DELETE':count=store.delete(library_key,paper_key,note_id)
        else:
            data=request.get_json(silent=True)
            if not isinstance(data,dict):raise ValueError('请求必须为 JSON 对象')
            count=store.update(library_key,paper_key,note_id,data.get('body'))
        if not count:abort(404)
        return jsonify(saved=True)

    return bp
