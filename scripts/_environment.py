"""Select the bootstrap interpreter environment without evaluating shell code.

Application settings are read exclusively by paper_library.config (python-dotenv).
The environment name is restricted to a portable Conda name, not arbitrary dotenv.
"""
import os
from pathlib import Path
import re

name=os.environ.get('PAPER_LIBRARY_CONDA_ENV')
if not name:
    path=Path(os.environ.get('PAPER_LIBRARY_SETTINGS_FILE',str(Path(__file__).resolve().parents[1]/'.env'))).expanduser()
    if path.is_file():
        for line in path.read_text(encoding='utf-8').splitlines():
            match=re.fullmatch(r'\s*(?:export\s+)?PAPER_LIBRARY_CONDA_ENV\s*=\s*[\"\']?([A-Za-z0-9_.-]+)[\"\']?\s*(?:#.*)?',line)
            if match:name=match[1]
name=name or 'paper'
if not re.fullmatch(r'[A-Za-z0-9_.-]+',name):raise SystemExit('PAPER_LIBRARY_CONDA_ENV 必须是环境名称')
print(name)
