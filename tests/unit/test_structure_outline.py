import json
from types import SimpleNamespace

import httpx
import pytest

from paper_library.config import TranslationSettings
from paper_library.structure.document import build_blocks,load_structure
from paper_library.structure.service import StructureService

SOURCE = '# Paper title\n\n# 1 Introduction\n\nContext and motivation.\n\n# Figure 1: results\n\n2 Method\n\nDetailed body text.\n\n# 3 Results\n'


def make_service(tmp_path, handler):
    settings=SimpleNamespace(translation=lambda:TranslationSettings('http://model/v1','qwen','',concurrency=2,budget_directory=tmp_path/'slots'),values={})
    return StructureService(settings,transport=httpx.MockTransport(handler))


def response(rows):
    return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'annotations':rows})}}]})


def local_rows(blocks):
    return [{'id':b['id'],'role':'heading','level':3} if b['type']=='heading_open' else {'id':b['id'],'role':'body'} for b in blocks]


def reviewed_rows(blocks):
    rows=[]
    for b in blocks:
        text=b['text'].strip().lstrip('# ')
        role='title' if text=='Paper title' else 'caption' if text.startswith('Figure') else 'heading' if text[:1].isdigit() else 'body'
        rows.append({'id':b['id'],'role':role,**({'level':1} if role=='heading' else {})})
    return rows


def test_structure_stage_reviews_the_whole_outline_and_caches_results(tmp_path):
    path=tmp_path/'article.md';path.write_text(SOURCE)
    calls=[]
    def handler(request):
        payload=json.loads(json.loads(request.content)['messages'][1]['content']);calls.append(payload)
        if payload.get('task')=='outline_review':
            assert payload['outline'] and payload['outline_scope']=='full'
            assert all('context_before' in b and 'context_after' in b for b in payload['blocks'])
            return response(reviewed_rows(payload['blocks']))
        return response(local_rows(payload['blocks']))
    service=make_service(tmp_path,handler);progress=[]
    result=service.generate(path,progress=lambda d,t:progress.append((d,t)))
    assert any(p.get('task')=='outline_review' for p in calls)
    assert result['outline_review']['status']=='completed'
    assert result['outline_review']['changes']
    rows={r['id']:r for r in result['annotations']}
    assert rows['source-7']['role']=='caption' and rows['source-7']['toc'] is False
    assert rows['source-9']=={'id':'source-9','role':'heading','level':1,'toc':True}
    assert path.read_text()==SOURCE
    assert load_structure(tmp_path,SOURCE)['annotations']==result['annotations']
    assert progress[-1][0]==progress[-1][1] and all(d<t for d,t in progress[:-1])
    before=len(calls);assert service.generate(path)==result;assert len(calls)==before


def test_failed_outline_keeps_old_sidecar_and_reuses_local_work_on_retry(tmp_path):
    path=tmp_path/'article.md';path.write_text(SOURCE)
    sidecar=tmp_path/'document-structure.json';sidecar.write_text('previous version')
    fail=True;local_calls=0
    def handler(request):
        nonlocal local_calls
        payload=json.loads(json.loads(request.content)['messages'][1]['content'])
        if payload.get('task')=='outline_review':
            return httpx.Response(503) if fail else response(reviewed_rows(payload['blocks']))
        local_calls+=1;return response(local_rows(payload['blocks']))
    service=make_service(tmp_path,handler)
    with pytest.raises(ValueError,match='目录'):service.generate(path)
    assert sidecar.read_text()=='previous version'
    fail=False;service.generate(path)
    assert local_calls==1


def test_uncertain_outline_keeps_previous_decision_and_is_rechecked(tmp_path):
    path=tmp_path/'article.md';path.write_text(SOURCE)
    reviews=0
    def handler(request):
        nonlocal reviews
        payload=json.loads(json.loads(request.content)['messages'][1]['content'])
        if payload.get('task')=='outline_review':
            reviews+=1;rows=reviewed_rows(payload['blocks'])
            if reviews==1:rows[1]={'id':rows[1]['id'],'role':'unknown'}
            return response(rows)
        return response(local_rows(payload['blocks']))
    service=make_service(tmp_path,handler)
    first=service.generate(path)
    assert first['outline_review']['uncertain']==['source-3']
    assert first['annotations'][1]['level']==3
    second=service.generate(path)
    assert reviews==2 and not second['outline_review']['uncertain']
    assert second['annotations'][1]['level']==1


def test_long_outline_windows_cover_every_candidate_with_bounded_context():
    from paper_library.structure.outline import candidates, review_batches
    source='\n\n'.join(f'# {i} 中文章节标题 ' + '研究方法'*20 for i in range(150))
    blocks=build_blocks(source)
    batches=list(review_batches(blocks,local_rows(blocks),1000))
    assert [b['id'] for _,targets in batches for b in targets]==[b['id'] for b in candidates(blocks,1000)]
    assert all(p['outline_scope']=='window' for p,_ in batches)
    assert all(len(json.dumps(p,ensure_ascii=False,separators=(',',':')))<=4000 for p,_ in batches)


def test_source_change_during_outline_review_does_not_replace_previous_structure(tmp_path):
    path=tmp_path/'article.md';path.write_text(SOURCE)
    sidecar=tmp_path/'document-structure.json';sidecar.write_text('previous version')
    def handler(request):
        payload=json.loads(json.loads(request.content)['messages'][1]['content'])
        if payload.get('task')=='outline_review':
            path.write_text(SOURCE+'\nNew content.')
            return response(reviewed_rows(payload['blocks']))
        return response(local_rows(payload['blocks']))
    with pytest.raises(ValueError,match='原文发生变化'):
        make_service(tmp_path,handler).generate(path)
    assert sidecar.read_text()=='previous version'
