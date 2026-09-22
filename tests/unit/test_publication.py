import multiprocessing
import os
from pathlib import Path
from paper_library.storage.publication import publish,recover_publications


def crash_publish(library):
    def replace_and_crash(source,target):
        os.replace(source,target)
        os._exit(23)
    publish(library,'p1',library/'papers'/'.stage',library/'new.tsv',replace_and_crash,lambda:None)


def test_process_death_after_catalog_replacement_restores_both_files(tmp_path):
    (tmp_path/'papers'/'p1').mkdir(parents=True)
    (tmp_path/'papers'/'p1'/'article.md').write_text('old paper')
    (tmp_path/'papers'/'.stage').mkdir()
    (tmp_path/'papers'/'.stage'/'article.md').write_text('new paper')
    (tmp_path/'catalog.tsv').write_text('old catalog')
    (tmp_path/'new.tsv').write_text('new catalog')
    child=multiprocessing.get_context('spawn').Process(target=crash_publish,args=(tmp_path,))
    child.start();child.join(10)
    try:assert child.exitcode==23
    finally:
        if child.is_alive():child.terminate();child.join()
    assert (tmp_path/'catalog.tsv').read_text()=='new catalog'
    recover_publications(tmp_path)
    assert (tmp_path/'catalog.tsv').read_text()=='old catalog'
    assert (tmp_path/'papers'/'p1'/'article.md').read_text()=='old paper'
    recover_publications(tmp_path)
    assert not list((tmp_path/'.transactions').iterdir())
