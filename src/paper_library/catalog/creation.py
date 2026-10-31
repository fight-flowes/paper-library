"""Create complete, discoverable libraries without exposing partial files."""
import fcntl
import json
from pathlib import Path
import shutil
import tempfile
from uuid import uuid4

from .models import CATALOG_COLUMNS


def create_library(repository, name):
    name = name.strip()
    if not name or len(name) > 120 or any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise ValueError('论文库名称须为 1–120 个字符，不能包含换行或控制字符')
    root = repository.root
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.libraries.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        libraries = repository.discover()
        if any(item.config.name.casefold() == name.casefold() for item in libraries.values()):
            raise ValueError('已存在同名论文库，请使用其他名称')
        while True:
            identifier = uuid4().hex[:12]
            library_id, prefix = 'library-' + identifier, 'l' + identifier
            if not (root / library_id).exists() and all(
                item.config.paper_id_prefix != prefix for item in libraries.values()
            ):
                break
        staging = Path(tempfile.mkdtemp(prefix='.new-library-', dir=root))
        try:
            (staging / 'library.toml').write_text(
                f'schema_version = 1\nid = "{library_id}"\n'
                f'name = {json.dumps(name, ensure_ascii=False)}\npaper_id_prefix = "{prefix}"\n',
                encoding='utf-8',
            )
            (staging / 'catalog.tsv').write_text('\t'.join(CATALOG_COLUMNS) + '\n', encoding='utf-8')
            (staging / 'papers').mkdir()
            staging.rename(root / library_id)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    return {'id': library_id, 'name': name}
