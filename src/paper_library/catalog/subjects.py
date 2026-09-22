"""Persistent per-library subjects, including topics without papers yet."""
import fcntl
import json
import os
import tempfile


def list_subjects(repository, library_id):
    snapshot = repository.load(library_id)
    path = snapshot.descriptor.root / 'subjects.json'
    saved = json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
    if not isinstance(saved, list) or any(not isinstance(item, str) for item in saved):
        raise ValueError('研究主题文件格式无效')
    return sorted(set(saved) | {p.subject for p in snapshot.papers if p.subject}, key=str.casefold)


def add_subject(repository, library_id, name):
    name = name.strip()
    if not name or len(name) > 120 or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ValueError('主题名称须为 1–120 个字符，不能包含换行或控制字符')
    root = repository.load(library_id).descriptor.root
    with (root / '.subjects.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        subjects = list_subjects(repository, library_id)
        existing = next((item for item in subjects if item.casefold() == name.casefold()), None)
        if existing:
            return existing
        subjects.append(name)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=root, delete=False) as handle:
                temporary = handle.name
                json.dump(sorted(subjects, key=str.casefold), handle, ensure_ascii=False, indent=2)
                handle.write('\n')
            os.replace(temporary, root / 'subjects.json')
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
    return name
