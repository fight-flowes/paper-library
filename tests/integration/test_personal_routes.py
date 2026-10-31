import json
from test_reader import make_settings
from paper_library.web.app import create_app
from paper_library.catalog import PaperPaths
from paper_library.storage.locking import gate


def test_paper_annotations_follow_text_position_not_creation_time(tmp_path):
    settings=make_settings(tmp_path)
    paths=PaperPaths.for_id(settings.paths.libraries/'example','ex-p0001')
    paths.markdown.write_text('# Example Paper\n\n# Abstract\n\nEarly phrase and late phrase.\n\nFollowing paragraph.')
    app=create_app(settings,start_jobs=False);client=app.test_client()
    endpoint='/api/reading/example/ex-p0001'
    version=client.get(endpoint).get_json()['document_version']
    headers={'X-Management-Token':app.extensions['management_guard'].token}
    from paper_library.personal.text import TextMap
    from paper_library.web.markdown import render_translatable_markdown
    blocks=TextMap(render_translatable_markdown(paths.markdown.read_text(),'/asset').html).blocks
    first=next(key for key,value in blocks.items() if 'Early phrase' in value)
    following=next(key for key,value in blocks.items() if 'Following paragraph' in value)
    for anchor,quote in [(first,'late phrase'),(following,'Following paragraph.'),(first,'Early phrase')]:
        assert client.post(endpoint,json={'document_version':version,'anchor':anchor,'quote':quote,
            'kind':'highlight','body':''},headers=headers).status_code==201
    rows=client.get(endpoint).get_json()['notes']
    assert [row['quote'] for row in rows]==['Early phrase','late phrase','Following paragraph.']


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


def test_highlight_action_is_in_toolbar_not_annotation_panel(tmp_path):
    from bs4 import BeautifulSoup
    app=create_app(make_settings(tmp_path),start_jobs=False)
    soup=BeautifulSoup(app.test_client().get('/library/example/paper/ex-p0001').data,'html.parser')
    button=soup.select_one('.paper-toolbar #add-highlight')
    assert button is not None and button.has_attr('disabled')
    assert button['aria-label']=='高亮选文' and button.select_one('svg')
    assert not soup.select('#notes-panel #add-highlight, #selected-quote, #note-text, #add-note')


def test_recent_reading_omits_redundant_highlight_label(tmp_path):
    from bs4 import BeautifulSoup
    app=create_app(make_settings(tmp_path),start_jobs=False);client=app.test_client()
    endpoint='/api/reading/example/ex-p0001';version=client.get(endpoint).get_json()['document_version']
    headers={'X-Management-Token':app.extensions['management_guard'].token}
    payload={'document_version':version,'anchor':'source-7','quote':'English paragraph.',
             'kind':'highlight','body':''}
    assert client.post(endpoint,json=payload,headers=headers).status_code==201
    soup=BeautifulSoup(client.get('/reading').data,'html.parser')
    card=soup.select_one('[data-note-kind="highlight"]')
    assert card is not None
    assert card.select_one('.note-kind') is None
    assert card.select_one('.note-body') is None
