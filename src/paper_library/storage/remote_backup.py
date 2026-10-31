"""Verified off-host backup archives using the existing cicore01 SSH transport."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import tarfile
import tempfile
import time
from uuid import uuid4

from .backup import verify_backup


WORKER = r'''
import fcntl,hashlib,json,os,re,shutil,sys,tarfile,time
from pathlib import Path,PurePosixPath
action,directory,name,manifest_hash,archive_hash,upload,keep=sys.argv[1:]
def digest(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
def sync(path):
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)
def write(path,data):
    temp=path.with_name(path.name+'.tmp')
    with temp.open('w') as stream:json.dump(data,stream);stream.flush();os.fsync(stream.fileno())
    os.replace(temp,path);sync(path.parent)
def verify(path):
    with tarfile.open(path,'r:gz') as archive:
        members=archive.getmembers()
        if len([m for m in members if m.name=='manifest.json'])!=1:raise ValueError('备份清单缺失或重复')
        member=archive.getmember('manifest.json')
        if not member.isfile() or member.size>32*1024**2:raise ValueError('备份清单格式无效')
        raw=archive.extractfile(member).read()
        if hashlib.sha256(raw).hexdigest()!=manifest_hash:raise ValueError('远程备份清单不匹配')
        manifest=json.loads(raw);files=manifest['files'];seen=set()
        if manifest.get('schema')!=1 or not isinstance(files,dict):raise ValueError('备份格式无效')
        for item in members:
            if item.name in seen or not item.isfile():raise ValueError('备份包含重复或非普通文件')
            seen.add(item.name)
            if item.name=='manifest.json':continue
            relative=PurePosixPath(item.name)
            if relative.is_absolute() or '..' in relative.parts or '\\' in item.name or relative.parts[0] not in {'libraries','state','cache'}:raise ValueError('备份路径无效')
            expected=files.get(item.name)
            if not expected or item.size!=expected['size']:raise ValueError('备份文件大小不匹配')
            with archive.extractfile(item) as stream:
                if hashlib.file_digest(stream,'sha256').hexdigest()!=expected['sha256']:raise ValueError('远程备份文件校验失败')
        if seen!=set(files)|{'manifest.json'}:raise ValueError('远程备份文件不完整')
        return dict(files=len(files),bytes=sum(f['size'] for f in files.values()),created_at=manifest['created_at'])
try:
    if not 1<=int(keep)<=365:raise ValueError('远程备份保留数量无效')
    root=Path(directory)
    if not root.is_absolute() or '..' in root.parts or root==Path('/') or any(p.is_symlink() for p in [root,*root.parents]):raise ValueError('远程备份目录不安全')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*',name):raise ValueError('备份名称不安全')
    if not re.fullmatch(r'\.upload-[a-f0-9]{32}',upload):raise ValueError('上传名称不安全')
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    if root.stat().st_uid!=os.getuid():raise ValueError('远程备份目录不属于当前用户')
    with (root/'.backup.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        marker=root/'.paper-library-backups.json'
        if not marker.exists():
            if any(p.name!='.backup.lock' for p in root.iterdir()):raise ValueError('远程目录非空，请使用专用备份目录')
            write(marker,{'kind':'paper-library-verified-backups','schema':1})
        if json.loads(marker.read_text()).get('kind')!='paper-library-verified-backups':raise ValueError('远程备份目录标记不匹配')
        root.chmod(0o700)
        target=root/(name+'.tar.gz');receipt=root/(name+'.receipt.json');staging=root/upload
        if action=='cleanup':
            if staging.is_file() and not staging.is_symlink():staging.unlink()
            print(json.dumps({'cleaned':True}))
        elif action in {'prepare','commit'}:
            reused=target.exists()
            if reused:
                if target.is_symlink():raise ValueError('远程备份不允许符号链接')
                details=verify(target)
                checksum=digest(target)
            elif action=='prepare':
                if shutil.disk_usage(root).free<1024**3:raise ValueError('远程备份磁盘空间不足')
                for path in root.glob('.upload-*'):
                    if path.is_file() and not path.is_symlink() and path.stat().st_mtime<time.time()-172800:path.unlink()
                print(json.dumps({'reused':False}));sys.exit(0)
            else:
                if staging.is_symlink() or digest(staging)!=archive_hash:raise ValueError('远程备份传输校验失败')
                details=verify(staging);checksum=archive_hash
                with staging.open('rb') as stream:os.fsync(stream.fileno())
                os.rename(staging,target);target.chmod(0o600);sync(root)
            write(receipt,dict(schema=1,manifest_sha256=manifest_hash,archive_sha256=checksum,**details))
            # Prune only verified archives in this application's marked directory.
            if action=='commit':
                candidates=[]
                for record in root.glob('*.receipt.json'):
                    try:
                        value=json.loads(record.read_text());archive=root/(record.name[:-len('.receipt.json')]+'.tar.gz')
                        if value.get('schema')==1 and archive.is_file() and not archive.is_symlink() and digest(archive)==value.get('archive_sha256'):
                            candidates.append((value['created_at'],archive,record))
                    except (OSError,ValueError,KeyError):continue
                for _,archive,record in sorted(candidates,reverse=True)[int(keep):]:archive.unlink();record.unlink()
            print(json.dumps(dict(reused=reused,archive_sha256=checksum,**details)))
        else:raise ValueError('远程备份操作无效')
except Exception as error:
    print(json.dumps({'error':str(error)}));sys.exit(1)
'''


def _remote_call(transport, python, arguments):
    command=[transport.ssh,*transport.options,'-p',transport.port,transport.target,shlex.join([python,'-c',WORKER,*arguments])]
    try:
        result=subprocess.run(command,env=transport.environment,stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=300)
        data=json.loads(result.stdout)
    except (OSError,subprocess.TimeoutExpired,ValueError) as error:
        raise ValueError('cicore01 远程备份连接或响应异常，本地备份已保留') from error
    if result.returncode or not isinstance(data,dict) or data.get('error'):
        message=data.get('error','SSH 操作失败') if isinstance(data,dict) else 'SSH 操作失败'
        raise ValueError('cicore01 远程备份失败：'+str(message)[:240])
    return data


def mirror_backup_remote(settings, source, *, transport_factory=None):
    from paper_library.ingest.remote import Transport
    source=Path(source).resolve();verify_backup(source)
    directory=settings.values.get('PAPER_BACKUP_REMOTE_DIR','').rstrip('/')
    if not directory or not PurePosixPath(directory).is_absolute() or '..' in PurePosixPath(directory).parts or not re.fullmatch(r'/[A-Za-z0-9_./-]+',directory):
        raise ValueError('PAPER_BACKUP_REMOTE_DIR 必须是无特殊字符的绝对路径')
    name=source.name
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*',name):raise ValueError('备份名称不安全')
    manifest_hash=hashlib.sha256((source/'manifest.json').read_bytes()).hexdigest()
    # Parser preferences must never redirect an existing off-host backup.
    connection_values=settings.base_values or settings.values
    upload='.upload-'+uuid4().hex;python=connection_values['PAPER_MINERU_REMOTE_PYTHON']
    arguments=[directory,name,manifest_hash,'',upload,str(int(settings.values.get('PAPER_BACKUP_KEEP','7')))]
    with tempfile.TemporaryDirectory(prefix='paper-remote-backup-') as temporary:
        transport=(transport_factory or Transport)(connection_values,temporary)
        transport.deadline=time.monotonic()+int(settings.values.get('PAPER_BACKUP_REMOTE_TIMEOUT_SECONDS','1800'))
        result=_remote_call(transport,python,['prepare',*arguments])
        if result.get('reused'):return dict(server='cicore01',directory=directory,backup=name,**result)
        try:
            archive=Path(temporary)/(name+'.tar.gz')
            with tarfile.open(archive,'w:gz',compresslevel=3) as stream:
                for path in sorted(source.rglob('*')):
                    if path.is_file():stream.add(path,arcname=str(path.relative_to(source)),recursive=False)
            with archive.open('rb') as stream:arguments[3]=hashlib.file_digest(stream,'sha256').hexdigest()
            transport.copy(archive,transport.target+':'+directory+'/'+upload)
            result=_remote_call(transport,python,['commit',*arguments])
            return dict(server='cicore01',directory=directory,backup=name,**result)
        finally:
            try:_remote_call(transport,python,['cleanup',*arguments])
            except ValueError:pass
