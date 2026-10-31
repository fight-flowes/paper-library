"""Recoverable publication of a paper package and its catalog.

The journal and recovery copies survive failed rollback and process termination.
Callers hold the library catalog lock for publish/recover.
"""
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path, value):
    temp = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    with temp.open('w', encoding='utf-8') as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    sync_directory(path.parent)


def _restore(transaction, library, record):
    destination = library / 'papers' / record['paper']
    staging = library / 'papers' / record['staging']
    old = transaction / 'old-package'
    if old.exists():
        if destination.exists():
            failed = transaction / 'failed-package'
            if failed.exists():
                shutil.rmtree(failed)
            os.replace(destination, failed)
        os.replace(old, destination)
        sync_directory(destination.parent)
    elif not record['had_package'] and not staging.exists() and destination.exists():
        os.replace(destination, transaction / 'failed-package')
    saved_catalog = transaction / 'catalog.tsv'
    if saved_catalog.exists():
        temp = library / ('.catalog-restore-' + transaction.name)
        shutil.copy2(saved_catalog, temp)
        with temp.open('rb') as stream:
            os.fsync(stream.fileno())
        os.replace(temp, library / 'catalog.tsv')
        sync_directory(library)
    if staging.exists():
        shutil.rmtree(staging)
    # Only reached after successful restoration. Any exception retains the journal.
    shutil.rmtree(transaction)


def recover_publications(library):
    directory = library / '.transactions'
    for transaction in sorted(directory.glob('*')):
        if not transaction.is_dir() or transaction.is_symlink():
            continue
        journal = transaction / 'journal.json'
        if not journal.exists():
            # No mutation can precede the durable journal.
            shutil.rmtree(transaction)
            continue
        record = json.loads(journal.read_text())
        if not isinstance(record, dict) or record.get('state') not in ('prepared', 'committed'):
            raise ValueError('发布恢复记录格式无效')
        if record.get('kind') == 'files':
            _file_targets(library, record)
            if record['state'] == 'committed':
                shutil.rmtree(transaction)
            else:
                _restore_files(transaction, library, record)
            continue
        if record.get('kind') not in (None, 'package') or type(record.get('had_package')) is not bool:
            raise ValueError('论文包恢复记录格式无效')
        for key in ('paper', 'staging'):
            value = record.get(key)
            if not isinstance(value, str) or not value or Path(value).name != value or value in {'.', '..'}:
                raise ValueError('发布恢复记录包含不安全路径')
        if record['state'] == 'committed':
            shutil.rmtree(transaction)
        else:
            _restore(transaction, library, record)


def publish(library, paper, staging, catalog_staging, publisher, validate):
    directory = library / '.transactions'
    directory.mkdir(exist_ok=True)
    transaction = directory / uuid4().hex
    transaction.mkdir()
    destination = library / 'papers' / paper
    record = dict(paper=paper, staging=staging.name, had_package=destination.exists(), state='prepared')
    shutil.copy2(library / 'catalog.tsv', transaction / 'catalog.tsv')
    # Flush prepared artifacts before making any live changes.
    for path in [catalog_staging, transaction / 'catalog.tsv', *staging.rglob('*')]:
        if path.is_file():
            with path.open('rb') as stream:
                os.fsync(stream.fileno())
    for path in sorted((p for p in staging.rglob('*') if p.is_dir()), key=lambda p:len(p.parts), reverse=True):
        sync_directory(path)
    sync_directory(staging)
    write_json(transaction / 'journal.json', record)
    sync_directory(directory)
    sync_directory(library)
    try:
        if destination.exists():
            os.replace(destination, transaction / 'old-package')
            sync_directory(transaction)
            sync_directory(destination.parent)
        os.replace(staging, destination)
        sync_directory(destination.parent)
        publisher(catalog_staging, library / 'catalog.tsv')
        sync_directory(library)
        validate()
        record['state'] = 'committed'
        write_json(transaction / 'journal.json', record)
    except BaseException:
        _restore(transaction, library, record)
        raise
    # Cleanup failure does not invalidate a committed publication.
    shutil.rmtree(transaction, ignore_errors=True)


def _file_targets(library, record):
    from paper_library.catalog.models import PAPER_KEY_PATTERN
    paper = record.get('paper')
    names = record.get('files')
    if (not isinstance(paper, str) or not PAPER_KEY_PATTERN.fullmatch(paper)
            or not isinstance(names, list) or not names
            or any(not isinstance(name, str) or name not in {'catalog.tsv', 'metadata.json', 'article.md'} for name in names)
            or len(set(names)) != len(names)):
        raise ValueError('元信息恢复记录包含不安全路径')
    targets = {name: library / 'catalog.tsv' if name == 'catalog.tsv' else library / 'papers' / paper / name for name in names}
    if any(path.is_symlink() or path.parent.is_symlink() for path in targets.values()):
        raise ValueError('元信息发布路径不允许符号链接')
    return targets


def _restore_files(transaction, library, record):
    for name, target in reversed(list(_file_targets(library, record).items())):
        saved = transaction / 'old' / name
        original = saved.read_bytes()
        if target.is_file() and target.read_bytes() == original:
            continue
        restore = transaction / ('restore-' + name)
        with restore.open('wb') as stream:
            stream.write(original);stream.flush();os.fsync(stream.fileno())
        os.replace(restore, target)
        sync_directory(target.parent)
    shutil.rmtree(transaction)
    sync_directory(library / '.transactions')


def publish_files(library, paper, replacements, validate):
    """Commit a bounded set of metadata files with idempotent crash recovery."""
    record = dict(kind='files', paper=paper, files=list(replacements), state='prepared')
    targets = _file_targets(library, record)
    directory = library / '.transactions';directory.mkdir(exist_ok=True)
    transaction = directory / uuid4().hex;transaction.mkdir()
    for child in ('old', 'new'):
        (transaction / child).mkdir()
    try:
        for name, target in targets.items():
            for child, data in [('old', target.read_bytes()), ('new', replacements[name])]:
                with (transaction / child / name).open('wb') as stream:
                    stream.write(data);stream.flush();os.fsync(stream.fileno())
        for child in ('old', 'new'):
            sync_directory(transaction / child)
        write_json(transaction / 'journal.json', record)
        sync_directory(directory)
        for name, target in targets.items():
            os.replace(transaction / 'new' / name, target)
            sync_directory(target.parent)
        validate()
        record['state'] = 'committed';write_json(transaction / 'journal.json', record)
    except BaseException:
        if (transaction / 'journal.json').exists():
            _restore_files(transaction, library, record)
        else:
            shutil.rmtree(transaction)
        raise
    shutil.rmtree(transaction, ignore_errors=True)
