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
        for key in ('paper', 'staging'):
            if Path(record[key]).name != record[key] or record[key] in {'.', '..'}:
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
