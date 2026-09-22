from types import SimpleNamespace

from paper_library.cli import guide


def test_batch_continues_after_paper_failure(tmp_path, monkeypatch, capsys):
    snapshot = SimpleNamespace(
        descriptor=SimpleNamespace(root=tmp_path),
        papers=[SimpleNamespace(id='p1'), SimpleNamespace(id='p2')])
    repository = SimpleNamespace(discover=lambda: ['topic'], load=lambda _: snapshot)
    monkeypatch.setattr(guide, 'default_settings', lambda: SimpleNamespace(paths=SimpleNamespace(libraries=tmp_path)))
    monkeypatch.setattr(guide, 'LibraryRepository', lambda _: repository)
    monkeypatch.setattr(guide.PaperPaths, 'for_id', lambda root, identifier: SimpleNamespace(
        markdown=root / identifier / 'article.md', root=root / identifier))
    calls = []

    def generate(path, **kwargs):
        calls.append(path.parent.name)
        if path.parent.name == 'p1':
            raise ValueError('引用未能核实')
        return {'sections': []}

    monkeypatch.setattr(guide, 'GuideService', lambda _: SimpleNamespace(generate=generate))
    assert guide.main(['run', '--all']) == 1
    assert calls == ['p1', 'p2']
    output = capsys.readouterr()
    assert '完成：' in output.out
    assert '失败 1 篇：topic/p1' in output.err
    assert '引用未能核实' in output.err
