"""Local diagnostics; optional dependencies never gate cached reading."""
from contextlib import closing
from datetime import datetime,timezone
import json
from pathlib import Path
import shutil
import sqlite3

import httpx

from paper_library.catalog import LibraryRepository,LibraryError
from paper_library.storage.backup import read_backup_status


def reading_status(settings, *, repository=None):
    checks={};ready=True
    try:
        repository=repository or LibraryRepository(settings.paths.libraries)
        issues=repository.validate_all();checks['library']={'status':'warning' if issues else 'ok','issues':list(issues)}
        if issues:ready=False
    except (LibraryError,OSError):checks['library']={'status':'error'};ready=False
    for name in ('jobs.sqlite3','personal.sqlite3'):
        path=settings.paths.state/name
        if not path.exists():checks[name]={'status':'not_created'};ready=False;continue
        try:
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
                valid=db.execute('PRAGMA quick_check').fetchone()[0]=='ok'
            checks[name]={'status':'ok' if valid else 'error'};ready=ready and valid
        except sqlite3.Error:checks[name]={'status':'error'};ready=False
    return {'reading_ready':ready,'checks':checks}


def check(settings,*,network=False):
    report=reading_status(settings)
    checks=report['checks'];ready=report['reading_ready']
    usage=shutil.disk_usage(settings.paths.root)
    minimum=int(settings.values.get('PAPER_MIN_FREE_MB','1024'))*1024*1024
    checks['disk']={'status':'warning' if usage.free<minimum else 'ok','free_mb':usage.free//1024//1024}
    backup_root=Path(settings.values.get('PAPER_BACKUP_DIR',str(settings.paths.root/'.backups'))).expanduser()
    latest=None;latest_date=None
    for manifest in backup_root.glob('*/manifest.json'):
        try:
            stamp=json.loads(manifest.read_text())['created_at']
            if not isinstance(stamp,str):continue
            candidate=datetime.fromisoformat(stamp)
            if candidate.tzinfo is None:continue
            if latest_date is None or candidate>latest_date:latest=stamp;latest_date=candidate
        except (OSError,ValueError,KeyError,TypeError):continue
    backup_status='not_created'
    if latest:
        try:
            age=(datetime.now(timezone.utc)-datetime.fromisoformat(latest)).total_seconds()
            backup_status='stale' if age>int(settings.values.get('PAPER_BACKUP_INTERVAL_HOURS','24'))*7200 else 'available'
        except (ValueError,TypeError):backup_status='error'
    checks['backup']={'status':backup_status,'latest':latest,'verification':'unknown'}
    try:
        state=read_backup_status(settings)
        checks['backup']['verification']=state.get('status','unknown')
        checks['backup']['verified_at']=state.get('verified_at')
        if state.get('status')=='error':checks['backup']['status']='error'
        if settings.values.get('PAPER_BACKUP_REMOTE_DIR'):
            mirror=state.get('remote_mirror',{})
            checks['backup_remote']={'status':'available' if mirror.get('status')=='ok' else 'unavailable',
                                     'server':'cicore01','latest':mirror.get('verified_at')}
    except (OSError,ValueError):pass
    if settings.values.get('PAPER_BACKUP_REMOTE_DIR') and 'backup_remote' not in checks:
        checks['backup_remote']={'status':'unavailable','server':'cicore01'}
    from paper_library.ingest.runtime import parser_command
    try:
        parser_command(settings.values);checks['mineru']={'status':'configured','execution':'remote','server':'cicore01','local_fallback':False}
    except ValueError:checks['mineru']={'status':'unavailable'}
    model=settings.translation(required=False)
    checks['model']={'status':'configured' if model.configured else 'unconfigured','model':model.model}
    checks['tavily']={'status':'not_probed'}
    if network:
        if checks['mineru']['status'] == 'configured':
            import tempfile
            import subprocess
            import shlex
            from paper_library.ingest.remote import Transport,identity_arguments
            try:
                with tempfile.TemporaryDirectory(prefix='paper-remote-check-') as temporary:
                    transport = Transport(settings.values, temporary)
                    result = subprocess.run([
                        transport.ssh, *transport.options, '-p', transport.port, transport.target,
                        shlex.join(identity_arguments(settings.values,check_worker=True)),
                    ], env=transport.environment, stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=25)
                    checks['mineru']['status'] = 'reachable' if result.returncode == 0 else 'unavailable'
            except (OSError, ValueError, subprocess.TimeoutExpired):
                checks['mineru']['status'] = 'unavailable'
        with httpx.Client(timeout=httpx.Timeout(5,connect=2),trust_env=False) as client:
            if model.configured:
                try:
                    response=client.get(model.base_url+'/models',headers={'Authorization':'Bearer '+model.api_key} if model.api_key else {})
                    checks['model']['status']='reachable' if response.is_success else 'unavailable'
                    checks['model']['http_status']=response.status_code
                except httpx.HTTPError:checks['model']['status']='unavailable'
            gateway=settings.values.get('TAVILY_GATEWAY_URL','http://127.0.0.1:18085').rstrip('/')
            try:
                response=client.get(gateway+'/health/ready')
                checks['tavily']={'status':'reachable' if response.is_success else 'unavailable','http_status':response.status_code}
            except httpx.HTTPError:checks['tavily']={'status':'unavailable'}
    return {'reading_ready':ready,'checked_at':datetime.now(timezone.utc).isoformat(),'checks':checks}
