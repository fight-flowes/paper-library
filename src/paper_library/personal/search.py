"""Rebuildable local full-text search including saved translations."""
from contextlib import contextmanager
import hashlib
from html.parser import HTMLParser
import json
import sqlite3
import fcntl
import os
from uuid import uuid4
from threading import Event, Thread, Lock
import logging

from paper_library.catalog import PaperPaths
from paper_library.translation.service import TranslationError
from paper_library.web.markdown import render_translatable_markdown
from .text import TextMap,normalize


class Plain(HTMLParser):
    def __init__(self,text):
        super().__init__();self.parts=[];self.feed(text)
    def handle_data(self,text):self.parts.append(text)


class SearchIndex:
    def __init__(self,path):
        self.background=False;self.ready=False;self.errors=[];self._stop=Event();self._thread=None;self._sync_lock=Lock()
        self.path=path;self.enabled=False;self.fts=False
        self._initialize_safely()

    def _initialize_safely(self):
        try:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            with self.path.with_suffix('.rebuild.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                try:
                    self._initialize()
                except sqlite3.DatabaseError as error:
                    # Busy/read-only/full databases are never quarantined as corrupt.
                    code=getattr(error,'sqlite_errorcode',0) & 0xff
                    if code not in {sqlite3.SQLITE_CORRUPT,sqlite3.SQLITE_NOTADB}:
                        raise
                    quarantine=self.path.parent/'search-quarantine'/uuid4().hex
                    quarantine.mkdir(parents=True,mode=0o700)
                    for name in (self.path, self.path.with_name(self.path.name+'-wal'), self.path.with_name(self.path.name+'-shm')):
                        if name.exists():os.replace(name,quarantine/name.name)
                    logging.getLogger(__name__).warning('search_index_quarantined')
                    self._initialize()
                self.enabled=True
        except (OSError,sqlite3.Error):
            self.enabled=False;self.errors=['检索索引暂不可用，论文仍可阅读']
            logging.getLogger(__name__).error('search_index_initialization_unavailable')

    def _initialize(self):
        with self.connect() as db:
            result=db.execute('PRAGMA quick_check').fetchone()
            if result and result[0]!='ok':
                error=sqlite3.DatabaseError('corrupt search index');error.sqlite_errorcode=sqlite3.SQLITE_CORRUPT;raise error
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
              CREATE TABLE IF NOT EXISTS documents (identity TEXT PRIMARY KEY,library TEXT,paper TEXT,title TEXT,stamp TEXT);
              CREATE TABLE IF NOT EXISTS fragments (id INTEGER PRIMARY KEY,identity TEXT,anchor TEXT,origin TEXT,text TEXT);
              CREATE INDEX IF NOT EXISTS fragments_doc ON fragments(identity);
            ''')
            try:
                db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(text,content='fragments',content_rowid='id',tokenize='trigram')")
                db.executescript('''
                CREATE TRIGGER IF NOT EXISTS fragments_ai AFTER INSERT ON fragments BEGIN
                  INSERT INTO search_fts(rowid,text) VALUES(new.id,new.text); END;
                CREATE TRIGGER IF NOT EXISTS fragments_ad AFTER DELETE ON fragments BEGIN
                  INSERT INTO search_fts(search_fts,rowid,text) VALUES('delete',old.id,old.text); END;
                ''')
                self.fts=True
            except sqlite3.OperationalError:self.fts=False

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=20);db.row_factory=sqlite3.Row
        try:
            with db:yield db
        finally:db.close()

    def start(self,repository,service,interval=10):
        if self.background:return
        self.background=True
        def loop():
            while not self._stop.is_set():
                try:self.errors=self.sync(repository,service);self.ready=True
                except Exception:
                    self.errors=['索引更新暂不可用'];logging.getLogger(__name__).exception('search_index_sync_failed')
                self._stop.wait(interval)
        self._thread=Thread(target=loop,name='paper-search-index',daemon=True);self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:self._thread.join(timeout=5)

    def sync(self,repository,service):
        with self._sync_lock:
            if not self.enabled:self._initialize_safely()
            if not self.enabled:raise sqlite3.OperationalError('检索索引暂不可用')
            return self._sync(repository,service)

    def _sync(self,repository,service):
        active=[];errors=[]
        descriptors,issues=repository.scan() if hasattr(repository,'scan') else (repository.discover(),{})
        errors.extend(f'{key}：论文库暂不可用' for key in issues)
        for library in descriptors:
            try:snapshot=repository.load(library)
            except (ValueError,OSError,RuntimeError):
                errors.append(library+'：目录暂不可用');continue
            errors.extend(f'{library}：{message}' for message in getattr(snapshot,'warnings',()))
            for paper in snapshot.papers:
                identity=library+'/'+paper.id;active.append(identity)
                paths=PaperPaths.for_id(snapshot.descriptor.root,paper.id)
                try:
                    def file_stamp(path):
                        stat=path.stat()
                        return (stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)
                    stamp=hashlib.sha256(json.dumps(['readable-authors-v2',file_stamp(paths.markdown),file_stamp(paths.metadata) if paths.metadata.exists() else None,
                        paper.title,service.revision(identity),service.prompt_fingerprint,service.config.model,service.config.base_url]).encode()).hexdigest()
                    with self.connect() as db:
                        old=db.execute('SELECT stamp FROM documents WHERE identity=?',(identity,)).fetchone()
                    if old and old['stamp']==stamp:continue
                    source=paths.markdown.read_text(encoding='utf-8')
                    meta=paths.metadata.read_text(encoding='utf-8') if paths.metadata.exists() else ''
                    version=hashlib.sha256(source.encode()).hexdigest()
                    rendered=render_translatable_markdown(source,'/asset');text=TextMap(rendered.html)
                    try:
                        metadata=json.loads(meta or '{}');authors=metadata.get('bibliography',{}).get('authors',[])
                    except (ValueError,AttributeError):authors=[]
                    author_text = ' · '.join(author.strip() for author in authors if isinstance(author, str) and author.strip()) if isinstance(authors, list) else ''
                    rows=[(identity,'','metadata',paper.title + (' — ' + author_text if author_text else ''))]
                    rows.extend((identity,anchor,'original',body) for anchor,body in text.blocks.items() if body)
                    try:saved=service.cached_many(identity,version,rendered.segments)
                    except TranslationError:
                        saved={};errors.append(identity+'：译文缓存暂不可读');stamp+='-translation-unavailable'
                    for segment in rendered.segments:
                        if segment.id in saved:
                            body=normalize(''.join(Plain(segment.render_translation(saved[segment.id])).parts))
                            rows.append((identity,text.translations.get(segment.id,''),'translation',body))
                    with self.connect() as db:
                        db.execute('DELETE FROM fragments WHERE identity=?',(identity,))
                        db.execute('INSERT OR REPLACE INTO documents VALUES (?,?,?,?,?)',(identity,library,paper.id,paper.title,stamp))
                        db.executemany('INSERT INTO fragments(identity,anchor,origin,text) VALUES (?,?,?,?)',rows)
                except (OSError,ValueError) as exc:
                    errors.append(identity)
                    with self.connect() as db:
                        db.execute('DELETE FROM fragments WHERE identity=?',(identity,));db.execute('DELETE FROM documents WHERE identity=?',(identity,))
        with self.connect() as db:
            for row in db.execute('SELECT identity FROM documents').fetchall():
                if row['identity'] not in active:
                    db.execute('DELETE FROM fragments WHERE identity=?',(row['identity'],));db.execute('DELETE FROM documents WHERE identity=?',(row['identity'],))
        return errors

    def search(self,query,limit=60):
        return self.page(query,per_paper=limit)[0]

    def page(self,query,page=1,page_size=20,per_paper=60,*,library='',origin=''):
        if not self.enabled:raise sqlite3.OperationalError('检索索引暂不可用')
        query=normalize(query)[:200]
        if not query:return [],0,0
        page=max(1,int(page));page_size=max(1,min(50,int(page_size)));per_paper=max(1,min(200,int(per_paper)))
        if self.fts and len(query)>=3:
            where='f.id IN (SELECT rowid FROM search_fts WHERE search_fts MATCH ?)'
            value='"'+query.replace('"','""')+'"'
        else:where='instr(lower(f.text),lower(?))>0';value=query
        priority="CASE f.origin WHEN 'metadata' THEN 0 WHEN 'original' THEN 1 ELSE 2 END"
        base=' FROM fragments f JOIN documents d ON d.identity=f.identity WHERE '+where
        parameters=[value]
        if library:
            base+=' AND d.library=?';parameters.append(library)
        if origin in {'metadata','original','translation'}:
            base+=' AND f.origin=?';parameters.append(origin)
        with self.connect() as db:
            counts=db.execute('SELECT count(DISTINCT f.identity),count(*)'+base,parameters).fetchone()
            papers=db.execute('SELECT f.identity,MIN('+priority+') AS rank'+base+' GROUP BY f.identity ORDER BY rank,d.title,f.identity LIMIT ? OFFSET ?',
                              (*parameters,page_size,(page-1)*page_size)).fetchall()
            rows=[]
            for paper in papers:
                rows.extend(db.execute('SELECT d.library,d.paper,d.title,f.anchor,f.origin,f.text'+base+' AND f.identity=? ORDER BY '+priority+',f.id LIMIT ?',
                                       (*parameters,paper['identity'],per_paper)).fetchall())
        results=[]
        for row in rows:
            result=dict(row);text=result.pop('text');offset=max(0,text.casefold().find(query.casefold())-80)
            result['excerpt']=('…' if offset else '')+text[offset:offset+280]+('…' if len(text)>offset+280 else '')
            results.append(result)
        return results,counts[0],counts[1]
