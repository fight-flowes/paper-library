"""Checksummed, credential-free backups with isolated restore."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import tempfile
from uuid import uuid4

from .locking import gate


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def sqlite_copy(source, destination):
    destination.parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as src, closing(sqlite3.connect(destination)) as dst:
        src.backup(dst)
        if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('数据库完整性校验失败')


def _copy_tree(source,target):
    if not source.exists():return
    for path in source.rglob('*'):
        parts=path.relative_to(source).parts
        if any(p.endswith('.lock') or p in {'.paper-locks','.structure-cache','reading-guide-cache','__pycache__'}
               or p.startswith(('.paper-staging-','.version-')) for p in parts):continue
        if path.is_symlink():raise ValueError('备份目录内不允许符号链接')
        if path.is_file():
            out=target/path.relative_to(source);out.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,out)


def create_backup(settings, destination=None, *, wait_seconds=0):
    destination=Path(destination or settings.values.get('PAPER_BACKUP_DIR',str(settings.paths.root/'.backups'))).expanduser().resolve()
    for protected in (settings.paths.libraries,settings.paths.state):
        if destination.is_relative_to(protected.resolve()):raise ValueError('备份目录不能位于论文库或运行数据内')
    destination.mkdir(parents=True,exist_ok=True,mode=0o700)
    with gate(settings.paths.root,exclusive=True,blocking=wait_seconds > 0,timeout=wait_seconds):
        if any(settings.paths.libraries.glob("*/.transactions/*/journal.json")):
            raise ValueError("存在未恢复的论文发布事务，请先启动服务恢复后再备份")
        stage=Path(tempfile.mkdtemp(prefix='.backup-',dir=destination))
        try:
            _copy_tree(settings.paths.libraries,stage/'libraries')
            _copy_tree(settings.uploads_directory,stage/'state'/'uploads')
            for name in ('jobs.sqlite3','personal.sqlite3'):
                source=settings.paths.state/name
                if source.exists():sqlite_copy(source,stage/'state'/name)
            if settings.paths.cache.exists():sqlite_copy(settings.paths.cache,stage/'cache'/'translations.sqlite3')
            files={str(p.relative_to(stage)): {'sha256':digest(p),'size':p.stat().st_size} for p in stage.rglob('*') if p.is_file()}
            manifest={'schema':1,'created_at':datetime.now(timezone.utc).isoformat(),'files':files,
                      'excluded':['credentials','process files','logs','rebuildable caches/search index']}
            (stage/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
            verify_backup(stage)
            target=destination/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:8])
            os.rename(stage,target)
            return target
        finally:
            if stage.exists():shutil.rmtree(stage)


def verify_backup(root):
    root=Path(root).resolve()
    try:data=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    except (OSError,ValueError) as exc:raise ValueError('备份清单无法读取') from exc
    if not isinstance(data,dict) or data.get('schema')!=1 or not isinstance(data.get('files'),dict):raise ValueError('不支持的备份格式')
    for name,expected in data['files'].items():
        relative=PurePosixPath(name)
        if relative.is_absolute() or '..' in relative.parts or '\\' in name or not relative.parts or relative.parts[0] not in {'libraries','state','cache'}:raise ValueError('备份包含不安全路径')
        if not isinstance(expected,dict) or not isinstance(expected.get('sha256'),str) or type(expected.get('size')) is not int:
            raise ValueError('备份文件清单格式无效')
        path=root/name
        if any(p.is_symlink() for p in [path,*path.parents] if p!=root.parent):raise ValueError('备份包含符号链接')
        if not path.is_file() or path.stat().st_size!=expected.get('size') or digest(path)!=expected.get('sha256'):raise ValueError('备份校验失败：'+name)
    return data


def restore_backup(root,target):
    root=Path(root).resolve();target=Path(target).expanduser().resolve()
    manifest=verify_backup(root)
    if target.exists() or target.is_relative_to(root):raise ValueError('恢复目标必须是尚不存在的新目录')
    target.parent.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix='.restore-',dir=target.parent))
    try:
        (stage/'libraries').mkdir()
        (stage/'.paper-library-dev').mkdir()
        for name,expected in manifest['files'].items():
            relative=PurePosixPath(name)
            head={ 'libraries':'libraries','state':'.paper-library-dev','cache':'.paper-library-dev'}[relative.parts[0]]
            out=stage/head/Path(*relative.parts[1:]);out.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(root/name,out)
            if digest(out)!=expected['sha256']:raise ValueError('恢复时源备份发生变化')
        jobs=stage/'.paper-library-dev'/'jobs.sqlite3'
        if jobs.exists():
            with sqlite3.connect(jobs) as connection:
                connection.execute("UPDATE jobs SET state='failed', stage='failed', runner_id=NULL, lease_expires_at=NULL, error='由备份恢复，需手动确认后重试' WHERE state IN ('queued','running')")
        env={'PAPER_LIBRARY_LIBRARIES_DIR':str(target/'libraries'),'PAPER_LIBRARY_STATE_DIR':str(target/'.paper-library-dev'),
             'PAPER_TRANSLATION_CACHE':str(target/'.paper-library-dev'/'translations.sqlite3')}
        (stage/'restore.env').write_text(''.join(k+'='+json.dumps(v,ensure_ascii=False)+'\n' for k,v in env.items()),encoding='utf-8')
        (stage/'restore-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
        os.rename(stage,target)
        return target
    finally:
        if stage.exists():shutil.rmtree(stage)


def prune_backups(destination,keep):
    if keep<1:raise ValueError('至少保留一份备份')
    candidates=[]
    for path in Path(destination).glob('*'):
        if path.is_dir() and not path.is_symlink() and not path.name.startswith('.'):
            try:manifest=verify_backup(path)
            except ValueError:continue
            candidates.append((manifest['created_at'],path))
    for _,path in sorted(candidates,reverse=True)[keep:]:shutil.rmtree(path)


def mirror_backup(source, directory):
    """Publish a verified secondary copy; never prune a user directory here."""
    source = Path(source).resolve()
    directory = Path(directory).expanduser().resolve()
    if directory == source.parent or directory.is_relative_to(source):
        raise ValueError('镜像目录必须与主备份目录分开')
    verify_backup(source)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = directory / source.name
    if target.exists():
        verify_backup(target)
        return target
    stage = Path(tempfile.mkdtemp(prefix='.mirror-', dir=directory))
    try:
        shutil.copytree(source, stage, dirs_exist_ok=True)
        verify_backup(stage)
        os.rename(stage, target)
        return target
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def record_backup_status(settings, **values):
    from .publication import write_json
    path = settings.paths.state / 'backup-status.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        previous = json.loads(path.read_text())
    except (OSError, ValueError):
        previous = {}
    write_json(path, {**previous, **values, 'checked_at': datetime.now(timezone.utc).isoformat()})
