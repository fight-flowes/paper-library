"""Shared maintenance and paper locks for supported terminal writers."""
from contextlib import ExitStack
import runpy
import sys
from pathlib import Path

from paper_library.config import default_settings
from paper_library.storage.locking import gate,paper_lock
from paper_library.storage.history import checkpoint


def main():
    module,*arguments=sys.argv[1:]
    writes = {
        'library': {'ingest','refresh-metadata','repair-paragraphs'},
        'translate': {'run','retry-warnings'}, 'guide': {'run', 'reconcile'}, 'structure': {'run'},
    }
    command = arguments[0] if arguments else ''
    def invoke():
        sys.argv=[module,*arguments]
        runpy.run_module(module,run_name='__main__')
    if command not in writes.get(module.rsplit('.',1)[-1],set()) or any(a in arguments for a in ('--help','-h','--dry-run')):
        return invoke()
    settings=default_settings()
    from paper_library.catalog import LibraryRepository, PaperPaths
    repository=LibraryRepository(settings.paths.libraries)
    def option(name):
        for i,arg in enumerate(arguments):
            if arg==name and i+1<len(arguments):return arguments[i+1]
            if arg.startswith(name+'='):return arg.split('=',1)[1]
        return None
    def selected():
        library=option('--library');paper=option('--paper')
        libraries=[library] if library else list(repository.discover())
        result=[]
        for ident in libraries:
            snapshot=repository.load(ident)
            rows=[snapshot.find(paper)] if paper else snapshot.papers
            if paper and rows==[None]:raise ValueError('论文不存在')
            if command=='ingest':
                # Only a duplicate PDF can replace an existing paper package.
                import hashlib
                candidates=[Path(arg) for arg in arguments[1:] if arg.lower().endswith('.pdf') and Path(arg).is_file()]
                hashes={hashlib.sha256(path.read_bytes()).hexdigest() for path in candidates}
                rows=[row for row in rows if row.sha256 in hashes]
            result.extend(PaperPaths.for_id(snapshot.descriptor.root,row.id).root for row in rows)
        return sorted(result)
    with gate(settings.paths.root), ExitStack() as locks:
        for path in selected():
            if path.is_dir():
                locks.enter_context(paper_lock(path));checkpoint(path,'before '+module.rsplit('.',1)[-1])
        try:
            sys.argv=[module,*arguments]
            runpy.run_module(module,run_name='__main__')
        finally:
            for path in selected():
                if path.is_dir():checkpoint(path,'after '+module.rsplit('.',1)[-1])


if __name__=='__main__':
    try:main()
    except (ValueError,OSError) as exc:print(str(exc),file=sys.stderr);raise SystemExit(1)
