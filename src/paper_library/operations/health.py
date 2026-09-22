"""Local diagnostics; optional dependencies never gate cached reading."""
from contextlib import closing
from datetime import datetime,timezone
import json
from pathlib import Path
import shutil
import sqlite3

import httpx

from paper_library.catalog import LibraryRepository,LibraryError


def check(settings,*,network=False):
    checks={};ready=True
    try:
        repository=LibraryRepository(settings.paths.libraries)
        issues=repository.validate_all();checks['library']={'status':'warning' if issues else 'ok','issues':list(issues)}
        if issues:ready=False
    except (LibraryError,OSError):checks['library']={'status':'error'};ready=False
    for name in ('jobs.sqlite3','personal.sqlite3'):
        path=settings.paths.state/name
        if not path.exists():checks[name]={'status':'not_created'};continue
        try:
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
                valid=db.execute('PRAGMA quick_check').fetchone()[0]=='ok'
            checks[name]={'status':'ok' if valid else 'error'};ready=ready and valid
        except sqlite3.Error:checks[name]={'status':'error'};ready=False
    usage=shutil.disk_usage(settings.paths.root)
    minimum=int(settings.values.get('PAPER_MIN_FREE_MB','1024'))*1024*1024
    checks['disk']={'status':'warning' if usage.free<minimum else 'ok','free_mb':usage.free//1024//1024}
    backup_root=Path(settings.values.get('PAPER_BACKUP_DIR',str(settings.paths.root/'.backups'))).expanduser()
    latest=None
    for manifest in backup_root.glob('*/manifest.json'):
        try:
            stamp=json.loads(manifest.read_text())['created_at']
            if latest is None or stamp>latest:latest=stamp
        except (OSError,ValueError,KeyError,TypeError):continue
    backup_status='not_created'
    if latest:
        try:
            age=(datetime.now(timezone.utc)-datetime.fromisoformat(latest)).total_seconds()
            backup_status='stale' if age>int(settings.values.get('PAPER_BACKUP_INTERVAL_HOURS','24'))*7200 else 'available'
        except (ValueError,TypeError):backup_status='error'
    checks['backup']={'status':backup_status,'latest':latest,'verification':'unknown'}
    try:
        state=json.loads((settings.paths.state/'backup-status.json').read_text())
        checks['backup']['verification']=state.get('status','unknown')
        checks['backup']['verified_at']=state.get('verified_at')
        if state.get('status')=='error':checks['backup']['status']='error'
    except (OSError,ValueError):pass
    from paper_library.ingest.runtime import parser_command
    try:
        parser_command(settings.values);checks['mineru']={'status':'installed'}
    except ValueError:checks['mineru']={'status':'unavailable'}
    model=settings.translation(required=False)
    checks['model']={'status':'configured' if model.configured else 'unconfigured','model':model.model}
    checks['tavily']={'status':'not_probed'}
    if network:
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
