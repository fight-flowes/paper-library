from paper_library.personal.search import SearchIndex


def test_search_supports_chinese_english_and_literal_symbols(tmp_path):
    index=SearchIndex(tmp_path/'search.sqlite3')
    with index.connect() as db:
        db.execute('INSERT INTO documents VALUES (?,?,?,?,?)',('lib/p','lib','p','Paper','v'))
        db.execute('INSERT INTO fragments(identity,anchor,origin,text) VALUES (?,?,?,?)',('lib/p','source-3','original','English whitespace 中文空白符 x%_y'))
    assert index.search('WHITESPACE')[0]['anchor']=='source-3'
    assert index.search('中文空白符')[0]['paper']=='p'
    assert index.search('空白')[0]['paper']=='p'
    assert index.search('%_')[0]['paper']=='p'
    assert index.search('" OR *')==[]


def test_paper_pagination_does_not_hide_low_hit_papers(tmp_path):
    index=SearchIndex(tmp_path/'search.sqlite3')
    with index.connect() as db:
        for identity,count in [('a',65),('b',1),('c',1)]:
            db.execute('INSERT INTO documents VALUES (?,?,?,?,?)',(identity,'lib',identity,identity,'v'))
            db.executemany('INSERT INTO fragments(identity,anchor,origin,text) VALUES (?,?,?,?)',[(identity,str(i),'original','needle') for i in range(count)])
    rows,total,hits=index.page('needle',page_size=2)
    assert {r['paper'] for r in rows}=={'a','b'} and total==3 and hits==67
    assert {r['paper'] for r in index.page('needle',page=2,page_size=2)[0]}=={'c'}


def test_translation_update_reindexes_only_its_paper(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import paper_library.personal.search as module
    root=tmp_path/'library'
    for key in ('a','b'):
        directory=root/'papers'/key;directory.mkdir(parents=True)
        (directory/'article.md').write_text('Text for '+key)
    snapshot=SimpleNamespace(descriptor=SimpleNamespace(root=root),papers=[SimpleNamespace(id=k,title=k) for k in ('a','b')])
    repository=SimpleNamespace(discover=lambda:['lib'],load=lambda _:snapshot)
    revisions={'lib/a':0,'lib/b':0}
    service=SimpleNamespace(revision=lambda key:revisions[key],prompt_fingerprint='same',config=SimpleNamespace(model='test',base_url='http://model'),cached_many=lambda *args:{})
    render=module.render_translatable_markdown;calls=[]
    def counted(*args):calls.append(args[0]);return render(*args)
    monkeypatch.setattr(module,'render_translatable_markdown',counted)
    index=SearchIndex(tmp_path/'search.sqlite3')
    assert index.sync(repository,service)==[]
    assert len(calls)==2
    revisions['lib/a']+=1
    assert index.sync(repository,service)==[]
    assert calls==['Text for a','Text for b','Text for a']
