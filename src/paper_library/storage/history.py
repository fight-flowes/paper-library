"""Immutable, verified paper versions, with isolated recovery."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
from datetime import datetime, timezone

from .backup import digest

FILES = ('article.md', 'metadata.json', 'document-structure.json', 'reading-guide.json', 'source.pdf')


def checkpoint(root: Path, reason: str):
    files = [root / name for name in FILES if (root / name).is_file()]
    files += sorted(p for p in (root / 'images').rglob('*') if p.is_file())
    if not files:
        return None
    if any(p.is_symlink() for p in files):
        raise ValueError('版本文件不允许符号链接')
    hashes = {str(p.relative_to(root)): digest(p) for p in files}
    version = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    history = root / '.history'
    history.mkdir(exist_ok=True)
    target = history / version
    if target.exists():
        return version
    stage = Path(tempfile.mkdtemp(prefix='.version-', dir=history))
    try:
        for name, expected in hashes.items():
            output = stage / name
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / name, output)
            if digest(output) != expected:
                raise ValueError('创建版本时论文文件发生变化')
        (stage / 'manifest.json').write_text(json.dumps({
            'schema': 2, 'version': version, 'created_at': datetime.now(timezone.utc).isoformat(),
            'reason': reason, 'files': hashes,
        }, ensure_ascii=False, indent=2), encoding='utf-8')
        try:
            os.rename(stage, target)
        except FileExistsError:
            pass
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return version


def revisions(root):
    rows = []
    for path in (root / '.history').glob('*/manifest.json'):
        if path.parent.name.startswith('.'):
            continue
        rows.append(json.loads(path.read_text(encoding='utf-8')))
    return sorted(rows, key=lambda row: row['created_at'], reverse=True)


def restore_revision(root: Path, version: str, target: Path):
    """Restore exactly the recorded files; older text-only versions stay usable."""
    if not re.fullmatch(r'[0-9a-f]{64}', version):
        raise ValueError('版本编号无效')
    source = root / '.history' / version
    manifest = json.loads((source / 'manifest.json').read_text())
    target = target.expanduser().resolve()
    if target.exists() or target.is_relative_to(root.resolve()):
        raise ValueError('恢复目标必须是论文包以外尚不存在的新目录')
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.restore-version-', dir=target.parent))
    try:
        for name, expected in manifest['files'].items():
            relative = PurePosixPath(name)
            if relative.is_absolute() or '..' in relative.parts or '\\' in name or not relative.parts:
                raise ValueError('版本清单包含不安全路径')
            if name not in FILES and relative.parts[0] != 'images':
                raise ValueError('版本清单包含未知文件')
            original = source / name
            if not original.resolve().is_relative_to(source.resolve()) or original.is_symlink():
                raise ValueError('版本文件路径无效')
            output = stage / name
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, output)
            if digest(output) != expected:
                raise ValueError('版本文件校验失败：' + name)
        shutil.copy2(source / 'manifest.json', stage / 'restored-version.json')
        os.rename(stage, target)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return target
