"""Resolve parser commands identically for CLI, services and diagnostics."""
from pathlib import Path
import os
import shutil
import sys


def parser_command(values):
    configured = values.get('PAPER_MINERU_EXECUTABLE', '').strip()
    if configured:
        candidate = shutil.which(configured)
        if candidate:
            return candidate
        raise ValueError('PAPER_MINERU_EXECUTABLE 不存在或不可执行')
    sibling = Path(sys.executable).parent / 'mineru'
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return str(sibling)
    candidate = shutil.which('mineru')
    if candidate:
        return candidate
    raise ValueError('当前 Python 环境未安装 MinerU，或未配置解析器路径')
