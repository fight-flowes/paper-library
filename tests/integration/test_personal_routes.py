import json
from test_reader import make_settings
from paper_library.web.app import create_app
from paper_library.catalog import PaperPaths
from paper_library.storage.locking import gate


def test_notes_progress_search_export_and_auth(tmp_path):
    settings=make_settings(tmp_path);app=create_app(settings,start_jobs=False);client=app.test_client()
    token=app.extensions['management_guard'].token;headers={'X-Management-Token':token}
    endpoint='/api/reading/example/ex-p0001';data=client.get(endpoint).get_json()
    payload={'document_version':data['document_version'],'anchor':'source-7','quote':'English paragraph.','kind':'note','body':'<script>alert(1)</script>'}
    assert client.post(endpoint,json=payload).status_code==403
    assert client.post(endpoint,json=payload,headers={**headers,'Origin':'https://evil.invalid'}).status_code==403
    response=client.post(endpoint,json=payload,headers=headers);assert response.status_code==201
    note_id=response.get_json()['id']
    assert client.get(endpoint).get_json()['notes'][0]['status']=='attached'
    page=client.get('/reading').get_data(as_text=True);assert '<script>alert(1)</script>' not in page
    assert client.get('/search?q=English').status_code==200
    assert '#source-7' in client.get('/search?q=English').get_data(as_text=True)
    assert 'document_version' in client.get('/reading/export').get_data(as_text=True)
    assert client.post(endpoint,json={**payload,'action':'progress','percent':40,'offset':0.2},headers=headers).status_code==200
    assert client.get(endpoint).get_json()['progress']['percent']==40
    paths=PaperPaths.for_id(settings.paths.libraries/'example','ex-p0001');paths.markdown.write_text('# Changed\n\nNo matching text.')
    assert client.get(endpoint).get_json()['notes'][0]['status']=='orphaned'
    assert client.patch(endpoint+'/'+note_id,json={'body':'Updated'},headers=headers).status_code==200
    with gate(settings.paths.root,exclusive=True):
        assert client.delete(endpoint+'/'+note_id,headers=headers).status_code==503
    assert client.delete(endpoint+'/'+note_id,headers=headers).status_code==200


def test_status_is_readable_without_model(tmp_path):
    app=create_app(make_settings(tmp_path),start_jobs=False)
    assert app.test_client().get('/manage/status').status_code==200


def test_search_highlights_escape_html_and_literal_queries(tmp_path):
    app=create_app(make_settings(tmp_path),start_jobs=False)
    highlight=app.jinja_env.filters['highlight_query']
    assert str(highlight('<script>English ENGLISH</script>', 'english')) == '&lt;script&gt;<mark>English</mark> <mark>ENGLISH</mark>&lt;/script&gt;'
    assert str(highlight('a+b [x]', 'a+b')) == '<mark>a+b</mark> [x]'
    assert str(highlight('<img>', '<img>')) == '<mark>&lt;img&gt;</mark>'
    page=app.test_client().get('/search?q=English').get_data(as_text=True)
    assert '<mark>English</mark>' in page
    assert '#source-7' in page


def test_current_reading_labels_and_legacy_position_records(tmp_path):
    app=create_app(make_settings(tmp_path),start_jobs=False);client=app.test_client()
    endpoint='/api/reading/example/ex-p0001'
    version=client.get(endpoint).get_json()['document_version']
    headers={'X-Management-Token':app.extensions['management_guard'].token}
    # Existing data types remain readable even though their creation UI was removed.
    assert client.post(endpoint,json={'document_version':version,'anchor':'source-7','quote':'English paragraph.','kind':'bookmark','body':''},headers=headers).status_code==201
    reading=client.get('/reading').get_data(as_text=True)
    assert '<h2>标注</h2>' in reading and '导出标注' in reading and '位置标记' in reading
    for path in ['/', '/reading','/manage/status','/library/example/paper/ex-p0001']:
        page=client.get(path).get_data(as_text=True)
        for obsolete in ['书签与笔记','笔记与书签','阅读记录与笔记','书签当前位置','文内查找']:
            assert obsolete not in page
    assert client.get('/reading/export?format=markdown').get_data(as_text=True).startswith('# 阅读标注')
