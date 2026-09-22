"""Generate local user units; activation stays an explicit CLI operation."""
import os
from pathlib import Path
import sys


def write_units(settings,directory=None):
    directory=Path(directory or Path(os.environ.get('XDG_CONFIG_HOME',str(Path.home()/'.config')))/'systemd'/'user')
    directory.mkdir(parents=True,exist_ok=True)
    def quoted(value):return '"'+str(value).replace('\\','\\\\').replace('"','\\"').replace('%','%%')+'"'
    root=str(settings.paths.root).replace('%','%%');python=quoted(sys.executable)
    if '\n' in root or '\r' in root:raise ValueError('服务路径不能包含换行')
    parser_path=str(Path(sys.executable).parent)+os.pathsep+os.environ.get('PATH','/usr/bin:/bin')
    common=f'WorkingDirectory={root}\nEnvironment="PYTHONUNBUFFERED=1"\nEnvironment={quoted("PATH="+parser_path)}\n'
    units={
      'paper-library.service':f'''[Unit]
Description=Local paper library reader
After=network.target
StartLimitIntervalSec=600
StartLimitBurst=5
[Service]
Type=simple
{common}ExecStart={python} -m paper_library.web.app
Restart=on-failure
RestartSec=5
TimeoutStopSec=40
UMask=0077
[Install]
WantedBy=default.target
''',
      'paper-library-backup.service':f'''[Unit]
Description=Verified paper library backup
[Service]
Type=oneshot
{common}ExecStart={python} -m paper_library.cli.ops backup --scheduled
UMask=0077
''',
      'paper-library-backup.timer':'''[Unit]
Description=Check paper backup schedule hourly
[Timer]
OnCalendar=hourly
Persistent=true
RandomizedDelaySec=180
[Install]
WantedBy=timers.target
'''}
    for name,text in units.items():(directory/name).write_text(text,encoding='utf-8')
    return directory
