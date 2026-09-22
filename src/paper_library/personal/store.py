"""Personal notes and reading state; generated paper data never overwrites these."""
from contextlib import contextmanager
from datetime import datetime,timezone
import json
import sqlite3
from uuid import uuid4

from .text import normalize,matching_positions,relocate


class ProgressConflict(ValueError):
    pass


class PersonalStore:
    def __init__(self,path):
        self.path=path;path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
            CREATE TABLE IF NOT EXISTS notes (
                id TEXT PRIMARY KEY, library TEXT NOT NULL, paper TEXT NOT NULL, kind TEXT NOT NULL,
                anchor TEXT NOT NULL, quote TEXT NOT NULL, prefix TEXT NOT NULL, suffix TEXT NOT NULL,
                body TEXT NOT NULL, document_version TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS notes_paper ON notes(library,paper);
            CREATE TABLE IF NOT EXISTS progress (
                library TEXT NOT NULL,paper TEXT NOT NULL, value TEXT NOT NULL,updated_at TEXT NOT NULL,
                PRIMARY KEY(library,paper)
            );
            ''')

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=10);db.row_factory=sqlite3.Row
        try:
            with db:yield db
        finally:db.close()

    def create(self,library,paper,data,blocks,version):
        kind=data.get('kind','note')
        if kind not in {'note','highlight','bookmark'}:raise ValueError('不支持的标注类型')
        anchor=data.get('anchor');quote=data.get('quote');body=data.get('body','')
        prefix=data.get('prefix','');suffix=data.get('suffix','')
        if not all(isinstance(v,str) for v in (anchor,quote,body,prefix,suffix)):raise ValueError('标注格式无效')
        if data.get('document_version')!=version:raise ValueError('原文已经变化，请刷新后重新标注')
        quote=normalize(quote)
        if not quote or len(quote)>4000 or len(body)>20000 or len(prefix)>100 or len(suffix)>100:raise ValueError('标注为空或内容过长')
        if len(matching_positions(blocks.get(anchor,''),quote,prefix,suffix))!=1:raise ValueError('选中文字无法唯一定位，请重新选择')
        now=datetime.now(timezone.utc).isoformat();key=uuid4().hex
        with self.connect() as db:
            db.execute('INSERT INTO notes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                       (key,library,paper,kind,anchor,quote,prefix,suffix,body,version,now,now))
        return key

    def list(self,library=None,paper=None):
        with self.connect() as db:
            if library is None:return [dict(r) for r in db.execute('SELECT * FROM notes ORDER BY updated_at DESC')]
            return [dict(r) for r in db.execute('SELECT * FROM notes WHERE library=? AND paper=? ORDER BY created_at',(library,paper))]

    def update(self,library,paper,key,body):
        if not isinstance(body,str) or len(body)>20000:raise ValueError('笔记内容过长')
        with self.connect() as db:
            return db.execute('UPDATE notes SET body=?,updated_at=? WHERE id=? AND library=? AND paper=?',
                (body,datetime.now(timezone.utc).isoformat(),key,library,paper)).rowcount

    def delete(self,library,paper,key):
        with self.connect() as db:return db.execute('DELETE FROM notes WHERE id=? AND library=? AND paper=?',(key,library,paper)).rowcount

    def save_progress(self,library,paper,data,blocks,version):
        if data.get('document_version')!=version:raise ValueError('原文已经变化')
        anchor=data.get('anchor');quote=data.get('quote')
        if not isinstance(anchor,str) or not isinstance(quote,str) or not quote or len(quote)>300:raise ValueError('阅读位置无效')
        quote=normalize(quote)
        if quote not in blocks.get(anchor,''):raise ValueError('阅读位置无法定位')
        try:offset=float(data.get('offset',0));percent=float(data.get('percent',0))
        except (ValueError,TypeError):raise ValueError('阅读进度无效') from None
        if not 0<=offset<=1 or not 0<=percent<=100:raise ValueError('阅读进度无效')
        value={'anchor':anchor,'quote':quote,'offset':offset,'percent':percent,'document_version':version}
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT value FROM progress WHERE library=? AND paper=?',(library,paper)).fetchone()
            previous=json.loads(row['value']) if row else {}
            revision=previous.get('revision',0)
            expected=data.get('expected_revision',revision)
            if type(expected) is not int or expected!=revision:
                raise ProgressConflict('阅读位置已在其他页面更新，请先恢复最新位置')
            value['revision']=revision+1
            db.execute('INSERT OR REPLACE INTO progress VALUES (?,?,?,?)',
                (library,paper,json.dumps(value,ensure_ascii=False),datetime.now(timezone.utc).isoformat()))
        return value['revision']

    def progress(self,library,paper):
        with self.connect() as db:row=db.execute('SELECT value FROM progress WHERE library=? AND paper=?',(library,paper)).fetchone()
        return json.loads(row['value']) if row else None

    def shelf(self):
        with self.connect() as db:return [dict(r) for r in db.execute('SELECT library,paper,value,updated_at FROM progress ORDER BY updated_at DESC')]
