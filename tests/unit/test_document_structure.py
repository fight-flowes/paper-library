import hashlib
import json

import pytest

from paper_library.structure.document import build_blocks, validate_annotations, load_structure


def test_blocks_preserve_multilingual_special_text():
    source = '# 标题\r\n\r\n中文与 English $x^2$。\r\n\r\n    code()\r\n\r\n诗歌第一行  \r\n第二行\r\n\r\n$$\nx=1\n$$\n\n<table><tr><td>A</td></tr></table>\n\n- item\n'
    blocks = build_blocks(source)
    assert blocks[1]['text'] == '中文与 English $x^2$。\r\n'
    assert all(b['locked'] for b in blocks[2:])
    assert len({b['id'] for b in blocks}) == len(blocks)


def test_validation_requires_exact_ids_and_protects_special_blocks():
    blocks = build_blocks('Text paragraph.\n\n```py\nprint(1)\n```')
    rows = [{'id':b['id'], 'role':'body'} for b in blocks]
    result = validate_annotations(rows, blocks)
    assert result[1]['role'] == 'preserve'
    for bad in (rows[:1], rows + rows[:1], [{'id':'missing','role':'body'},rows[1]]):
        with pytest.raises(ValueError): validate_annotations(bad, blocks)
    with pytest.raises(ValueError): validate_annotations([{'id':blocks[0]['id'],'role':'<script>'},rows[1]],blocks)


def test_stale_and_corrupt_sidecars_fall_back(tmp_path):
    source = 'A paragraph.'
    path = tmp_path / 'document-structure.json'
    path.write_text('{broken')
    assert load_structure(tmp_path, source) is None
    path.write_text(json.dumps({'version':1,'source_sha256':'wrong','annotations':[]}))
    assert load_structure(tmp_path, source) is None


def test_annotations_preserve_translation_ids_and_text():
    from paper_library.web.markdown import render_markdown
    source = '# Paper\n\nAuthors\n\nAn untitled abstract with $x^2$.\n\n## Results\n\n正文。'
    blocks = build_blocks(source)
    rows = [{'id':b['id'],'role':'abstract' if 'untitled' in b['text'] else 'unknown'} for b in blocks]
    base = render_markdown(source, '/asset', translation_slots=True)
    styled = render_markdown(source, '/asset', translation_slots=True, annotations=rows)
    assert base.segments == styled.segments
    assert 'structure-abstract' in styled.html
    assert base.headings == styled.headings


def test_service_reuses_cache_and_rejects_changed_source(tmp_path):
    from types import SimpleNamespace
    import httpx
    from paper_library.config import TranslationSettings
    from paper_library.structure.service import StructureService
    path = tmp_path / 'article.md'
    path.write_text('# Title\n\n中文 English body.')
    config = TranslationSettings('http://model/v1', 'qwen', '', concurrency=2, temperature=0.1)
    settings = SimpleNamespace(translation=lambda:config, values={})
    calls = []
    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        blocks = json.loads(payload['messages'][1]['content'])['blocks']
        rows = [{'id':b['id'],'role':'body'} for b in blocks]
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'annotations':rows})}}]})
    service = StructureService(settings, transport=httpx.MockTransport(handler))
    progress = []
    first = service.generate(path, progress=lambda d,t:progress.append((d,t)))
    assert service.generate(path) == first
    assert len(calls) == 2 and all(call['temperature'] == 0.1 for call in calls)
    assert progress == [(0,4),(2,4),(4,4)]
    with pytest.raises(ValueError, match='取消'):
        service.generate(path, cancelled=lambda:True)
    path.write_text('Changed source')
    assert load_structure(tmp_path,path.read_text()) is None


def test_inflight_source_change_does_not_publish(tmp_path):
    from types import SimpleNamespace
    import httpx
    from paper_library.config import TranslationSettings
    from paper_library.structure.service import StructureService
    path = tmp_path / 'article.md'; path.write_text('Original body.')
    config = TranslationSettings('http://model/v1', 'qwen', '')
    def handler(request):
        blocks = json.loads(json.loads(request.content)['messages'][1]['content'])['blocks']
        path.write_text('Edited body.')
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'annotations':[{'id':b['id'],'role':'body'} for b in blocks]})}}]})
    service = StructureService(SimpleNamespace(translation=lambda:config,values={}),transport=httpx.MockTransport(handler))
    with pytest.raises(ValueError,match='原文发生变化'): service.generate(path)
    assert not (tmp_path/'document-structure.json').exists()


def test_invalid_model_output_does_not_replace_existing(tmp_path):
    from types import SimpleNamespace
    import httpx
    from paper_library.config import TranslationSettings
    from paper_library.structure.service import StructureService
    path = tmp_path/'article.md'; path.write_text('Original body.')
    output = tmp_path/'document-structure.json'; output.write_text('previous version')
    config = TranslationSettings('http://model/v1','qwen','')
    def handler(request):
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'{"annotations":[]}'}}]})
    service = StructureService(SimpleNamespace(translation=lambda:config,values={}),transport=httpx.MockTransport(handler))
    with pytest.raises(ValueError,match='未完成'): service.generate(path,force=True)
    assert output.read_text() == 'previous version'


def test_heading_changes_keep_original_source_anchor_and_translation():
    from paper_library.web.markdown import render_markdown
    source = '# **Title**\n\nA short subsection\n\nBody text.\n\n## Results'
    blocks = build_blocks(source)
    rows = [{'id':b['id'],'role':'unknown'} for b in blocks]
    rows[1].update(role='heading',level=3)
    rows[3].update(role='heading',level=2)
    base = render_markdown(source,'/asset',translation_slots=True)
    result = render_markdown(source,'/asset',translation_slots=True,annotations=rows)
    assert result.segments == base.segments
    assert 'id="source-3"' in result.html
    assert result.headings[0] == base.headings[0]
    assert result.headings[1].id == 'source-3'
    assert result.headings[1].level == 3


def test_nested_headings_and_invalid_annotations_preserve_rendering():
    from paper_library.web.markdown import render_markdown
    source = '# Title\n\n> ## Quoted heading\n> Example\n\n- **List** item\n\n## Results'
    base = render_markdown(source,'/asset',translation_slots=True)
    result = render_markdown(source,'/asset',translation_slots=True,annotations=[])
    assert base == result


def test_failed_force_run_resumes_instead_of_reusing_old_output(tmp_path):
    from types import SimpleNamespace
    from paper_library.config import TranslationSettings
    from paper_library.structure.service import StructureService
    path = tmp_path/'article.md'; path.write_text('Original body.')
    service = StructureService(SimpleNamespace(translation=lambda:TranslationSettings('http://model/v1','qwen',''),values={}))
    calls = []
    def ask(payload, blocks, cancelled):
        calls.append(1)
        return [{'id':b['id'],'role':'body'} for b in blocks]
    service._ask = ask
    service.generate(path)
    def fail(*args): raise ValueError('Unavailable')
    service._ask = fail
    with pytest.raises(ValueError): service.generate(path,force=True)
    # Failed forced regeneration must not silently return the old output/cache.
    progress = []
    service._ask = ask
    service.generate(path,progress=lambda d,t:progress.append((d,t)))
    assert len(calls) == 4
    assert progress[0] == (0,2)
    assert not (tmp_path/'.structure-cache'/'pending.json').exists()


def test_retry_unknown_rechecks_only_uncertain_batches(tmp_path):
    from types import SimpleNamespace
    from paper_library.config import TranslationSettings
    from paper_library.structure.service import StructureService
    path=tmp_path/'article.md'
    path.write_text('a'*700+'\n\n'+'b'*700)
    service=StructureService(SimpleNamespace(translation=lambda:TranslationSettings('http://model/v1','qwen',''),values={'PAPER_STRUCTURE_CHUNK_CHARS':'1000'}))
    calls=[]
    def ask(payload,blocks,cancelled):
        key=blocks[0]['id'];calls.append(key)
        role='unknown' if key=='source-1' and calls.count(key)==1 else 'body'
        return [{'id':b['id'],'role':role} for b in blocks]
    service._ask=ask
    service.generate(path);result=service.generate(path)
    assert calls.count('source-1')==2 and calls.count('source-3')==1
    assert all(r['role']=='body' for r in result['annotations'])


def test_reviewed_toc_removes_false_headings_without_losing_anchors_or_text():
    from paper_library.web.markdown import render_markdown
    from paper_library.reading.structure import build_document
    source = '# Paper title\n\n# 1 Introduction\n\nSome introduction text.\n\n# Figure 1: a caption\n\nExperimental setup\n\nSome setup details.\n'
    blocks = build_blocks(source)
    rows = [{'id':b['id'], 'role':'body', 'toc':False} for b in blocks]
    rows[0].update(role='title')
    rows[1].update(role='heading', level=1, toc=True)
    rows[3].update(role='caption')
    rows[4].update(role='heading', level=2, toc=True)
    baseline = render_markdown(source, '/asset', translation_slots=True)
    rendered = render_markdown(source, '/asset', translation_slots=True, annotations=rows)
    assert [h.text for h in rendered.headings] == ['1 Introduction', 'Experimental setup']
    assert rendered.segments == baseline.segments
    assert 'id="figure-1-a-caption"' in rendered.html
    assert '<p id="figure-1-a-caption"' in rendered.html
    assert 'Paper title' in rendered.html
    document = build_document(source, rows)
    assert any('Figure 1: a caption' in b['text'] for b in document['blocks'])
    assert next(s for s in document['sections'] if s['title']=='Experimental setup')['parent']=='1-introduction'
    assert all(f'id="{b["id"]}"' in rendered.html for b in document['blocks'])


def test_reviewed_toc_flags_are_validated_without_changing_legacy_annotations():
    blocks = build_blocks('# A heading\n\nBody.')
    legacy = [{'id':b['id'],'role':'body'} for b in blocks]
    assert validate_annotations(legacy, blocks) == legacy
    for toc in ('yes', True):
        with pytest.raises(ValueError):
            validate_annotations([{**legacy[0], 'toc':toc}, legacy[1]], blocks)
