"""Remote-only PDF parsing policy shared by CLI, web jobs and diagnostics."""
from pathlib import PurePosixPath
import shutil


def parser_command(values):
    if values.get('PAPER_MINERU_MODE', 'remote') != 'remote':
        raise ValueError('禁止在本机解析 PDF；仅支持 cicore01 远程解析')
    for key in ('HOST', 'USER', 'PYTHON', 'ROOT'):
        value = values.get('PAPER_MINERU_REMOTE_' + key, '').strip()
        if not value or any(ord(c) < 32 for c in value) or value.startswith('-'):
            raise ValueError('请配置 PAPER_MINERU_REMOTE_' + key + '；禁止回退本机解析')
        if key in {'PYTHON', 'ROOT'} and (
            not PurePosixPath(value).is_absolute() or '..' in PurePosixPath(value).parts
        ):
            raise ValueError('远程 Python 和工作目录必须使用绝对路径')
    try:
        port = int(values.get('PAPER_MINERU_REMOTE_PORT', '22'))
    except ValueError:
        raise ValueError('远程 SSH 端口必须是整数') from None
    if not 1 <= port <= 65535:
        raise ValueError('远程 SSH 端口超出范围')
    command = shutil.which('ssh')
    if not command or not shutil.which('scp'):
        raise ValueError('未安装 SSH/SCP 客户端；禁止回退本机解析')
    return command
