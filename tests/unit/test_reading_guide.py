import json

import pytest

from paper_library.reading.structure import build_document, chunk_section
from paper_library.reading.service import validate_points, load_guide, GuideService
from paper_library.web.markdown import render_translatable_markdown


def test_actual_titles_numeric_hierarchy_and_source_links():
    source = '# A Paper\n\nAuthors\n\n# 1 A Strange Question\n\nSome evidence here.\n\n# 1.1 Looking Closer\n\nMore evidence.\n\n# Looking Closer\n\nFinal text.\n'
    doc = build_document(source)
    assert [s['title'] for s in doc['sections']] == ['A Paper', '1 A Strange Question', '1.1 Looking Closer', 'Looking Closer']
    assert doc['sections'][2]['level'] == 2
    rendered = render_translatable_markdown(source, '/asset')
    for block in doc['blocks']:
        assert f'id="{block["id"]}"' in rendered.html


def test_code_headings_are_not_sections_and_no_heading_is_supported():
    doc = build_document('Prose.\n\n```\n# not a heading\n```\n')
    assert len(doc['sections']) == 1
    assert doc['sections'][0]['title'] == '正文'


def test_chunking_never_discards_long_block_tail():
    doc = build_document('# Unusual title\n\n' + '0123456789' * 2500)
    chunks = chunk_section(doc['sections'][0], 1000)
    parts = [block['text'] for chunk in chunks for block in chunk]
    assert ''.join(parts) == doc['sections'][0]['blocks'][0]['text']
    assert max(map(len, parts)) <= 1000


def test_evidence_requires_real_id_and_exact_quote():
    blocks = [{'id': 'source-3', 'text': 'Accuracy improves only on dataset A.'}]
    points = [{'text': '作者仅在数据集 A 上观察到改善。', 'kind': '原文结论',
               'evidence': [{'id': 'source-3', 'quote': 'only on dataset A'}]}]
    assert validate_points(points, blocks)[0]['text'] == points[0]['text']
    points[0]['evidence'][0]['quote'] = 'all datasets'
    with pytest.raises(ValueError):
        validate_points(points, blocks)
    points[0]['evidence'][0] = {'id': 'invented', 'quote': 'Accuracy'}
    with pytest.raises(ValueError):
        validate_points(points, blocks)


def test_quotes_match_rendered_html_entities_without_loosening_content():
    blocks = [{'id': 'source-1', 'text': '<td>It&#x27;s difficult to interpret &quot;input&quot;.</td>'}]
    point = {'kind': '原文结论', 'text': '难以解释输入', 'evidence': [
        {'id': 'source-1', 'quote': 'It\'s difficult to interpret "input".'}]}
    assert validate_points([point], blocks)
    point['evidence'][0]['quote'] = 'It is easy to interpret input.'
    with pytest.raises(ValueError):
        validate_points([point], blocks)


def test_table_quotes_match_visible_cells_but_reject_altered_numbers():
    blocks = [{'id': 'source-1', 'text': '<table><tr><td rowspan="4">Original</td><td>SST-2</td><td>96.67</td></tr></table>'}]
    point = {'kind': '原文结论', 'text': '原始输入的结果。', 'evidence': [
        {'id': 'source-1', 'quote': '<tr><td>SST-2</td><td>96.67</td></tr>'}]}
    assert validate_points([point], blocks)[0]['evidence'][0]['quote'] == 'SST-2 96.67'
    point['evidence'][0]['quote'] = 'SST-2 99.67'
    with pytest.raises(ValueError):
        validate_points([point], blocks)


def test_stale_guide_is_identified(tmp_path):
    path = tmp_path / 'reading-guide.json'
    path.write_text(json.dumps({'schema_version': 1, 'document_version': 'old', 'sections': [], 'overview': []}))
    assert load_guide(tmp_path, 'new')['stale'] is True


def test_generation_reuses_cache_and_does_not_publish_failures(tmp_path):
    import httpx
    from paper_library.config import Settings, ProjectPaths
    settings = Settings.load(ProjectPaths.from_root(tmp_path), {
        'PAPER_TRANSLATION_PROFILE': 'test', 'LLM_TEST_MODEL': 'test',
        'LLM_TEST_BASE_URL': 'http://model/v1', 'LLM_TEST_MAX_CONCURRENCY': '2'})
    path = tmp_path / 'article.md'
    path.write_text('# Unconventional Question\n\nThe evidence supports only dataset A.\n')
    calls = []

    def respond(request):
        payload = json.loads(json.loads(request.content)['messages'][1]['content'])
        calls.append(payload)
        if 'blocks' in payload:
            block = payload['blocks'][0]
            evidence = {'id': block['id'], 'quote': block['text']}
        else:
            evidence = payload['sections'][0]['points'][0]['evidence'][0]
        content = {'role': '分析研究证据', 'points': [{'kind': '原文结论', 'label': '适用条件',
                   'text': '证据仅覆盖数据集 A。', 'evidence': [evidence]}]}
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(content)}}]})

    service = GuideService(settings, transport=httpx.MockTransport(respond))
    result = service.generate(path, progress=lambda _: None)
    assert len(calls) == 2
    assert result['sections'][0]['title'] == 'Unconventional Question'
    service.generate(path, progress=lambda _: None)
    assert len(calls) == 2
    previous = (tmp_path / 'reading-guide.json').read_bytes()
    bad = GuideService(settings, transport=httpx.MockTransport(lambda request: httpx.Response(503)))
    with pytest.raises(ValueError):
        bad.generate(path, force=True, progress=lambda _: None)
    assert (tmp_path / 'reading-guide.json').read_bytes() == previous

    # A failed forced run resumes instead of silently returning the old guide.
    with pytest.raises(ValueError, match='HTTP 503'):
        bad.generate(path, progress=lambda _: None)
    service.generate(path, progress=lambda _: None)
    assert len(calls) == 4
    previous = (tmp_path / 'reading-guide.json').read_bytes()
    assert not list((tmp_path / 'reading-guide-cache').glob('*/pending.json'))

    def changing_source(request):
        response = respond(request)
        path.write_text(path.read_text() + '\nUpdated while generating.\n')
        return response

    changing = GuideService(settings, transport=httpx.MockTransport(changing_source))
    with pytest.raises(ValueError, match='原文已更新'):
        changing.generate(path, force=True, progress=lambda _: None)
    assert (tmp_path / 'reading-guide.json').read_bytes() == previous


def test_all_supported_source_blocks_have_rendered_anchors():
    source = '# H\n\n```\nx\n```\n\n$$x^2$$\n\n<table><tr><td>value</td></tr></table>\n\n- list entry\n'
    doc = build_document(source)
    rendered = render_translatable_markdown(source, '/asset')
    for block in doc['blocks']:
        assert f'id="{block["id"]}"' in rendered.html


def test_long_section_is_integrated_once_instead_of_concatenated(tmp_path):
    from paper_library.config import Settings, ProjectPaths
    settings = Settings.load(ProjectPaths.from_root(tmp_path), {
        'PAPER_TRANSLATION_PROFILE': 'test', 'LLM_TEST_MODEL': 'test',
        'LLM_TEST_BASE_URL': 'http://model/v1', 'PAPER_GUIDE_CHUNK_CHARS': '1000'})
    path = tmp_path / 'article.md'
    path.write_text('# How do the pieces fit?\n\n' + '\n\n'.join(
        f'Step {i}: ' + 'The conditions and observations are linked. ' * 15 for i in range(3)))
    service = GuideService(settings)
    calls = []

    def respond(payload, blocks):
        calls.append(payload)
        if payload['task'] == '章节导读':
            point = {'kind': '帮助理解', 'label': '步骤', 'text': blocks[0]['id'],
                     'evidence': [{'id': blocks[0]['id'], 'quote': blocks[0]['text'][:50]}]}
            return {'role': '解释步骤', 'points': [point], 'details': [], 'skip_reason': ''}
        if payload['task'] == '章节整合':
            source_points = [part['points'][0] for part in payload['parts']]
            return {'role': '串联三个步骤', 'points': [source_points[0]], 'details': source_points,
                    'skip_reason': ''}
        return {'role': '全文', 'points': payload['sections'][0]['points'], 'details': [], 'skip_reason': ''}

    service._ask = respond
    guide = service.generate(path, progress=lambda _: None)
    section = guide['sections'][0]
    assert section['source_parts'] == 3
    assert len(section['parts']) == 1
    assert section['parts'][0]['role'] == '串联三个步骤'
    assert len(section['parts'][0]['details']) == 3
    assert len([call for call in calls if call['task'] == '章节整合']) == 1


def test_detailed_explanation_requires_its_own_valid_evidence(tmp_path):
    import httpx
    from paper_library.config import Settings, ProjectPaths
    settings = Settings.load(ProjectPaths.from_root(tmp_path), {
        'PAPER_TRANSLATION_PROFILE': 'test', 'LLM_TEST_MODEL': 'test', 'LLM_TEST_BASE_URL': 'http://model/v1'})
    evidence = {'id': 'source-1', 'quote': 'The original observation is limited.'}
    point = {'kind': '原文结论', 'text': '原文观察有适用范围。', 'evidence': [evidence]}
    detail = {'kind': '帮助理解', 'text': '额外解释。', 'evidence': [{'id': 'source-1', 'quote': 'Invented explanation.'}]}
    value = {'role': '分析', 'points': [point], 'details': [detail]}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(value)}}]}))
    service = GuideService(settings, transport=transport)
    with pytest.raises(ValueError, match='导读生成失败'):
        service._ask({'task': '章节导读'}, [{'id': 'source-1', 'text': evidence['quote']}])
    detail['evidence'] = [evidence]
    assert len(service._ask({'task': '章节导读'}, [{'id': 'source-1', 'text': evidence['quote']}])['details']) == 1
    detail['text'] = point['text']
    assert service._ask({'task': '章节导读'}, [{'id': 'source-1', 'text': evidence['quote']}])['details'] == []


def test_quote_error_identifies_exact_quote_for_targeted_retry():
    blocks = [{'id': 'source-62', 'text': 'Prefix spac- ing can also be used in more unconventional ways.'}]
    point = {'kind': '原文结论', 'text': '缩进也有非常规用法。', 'evidence': [
        {'id': 'source-62', 'quote': 'Prefix spacing cannot be used in more unconventional ways.'}]}
    with pytest.raises(ValueError) as raised:
        validate_points([point], blocks)
    assert 'Prefix spacing cannot be used' in str(raised.value)
    assert 'Prefix spac- ing can also be used' in str(raised.value)


def test_quote_repairs_parser_word_breaks_without_accepting_changed_numbers():
    source = 'We eval- uate models and report 95.2 accuracy.'
    point = {'kind': '原文结论', 'text': '模型评估。', 'evidence': [
        {'id': 'source-95', 'quote': 'evaluate models and report 95.2 accuracy.'}]}
    blocks = [{'id': 'source-95', 'text': source}]
    checked = validate_points([point], blocks)
    assert checked[0]['evidence'][0]['quote'] == 'eval- uate models and report 95.2 accuracy.'
    point['evidence'][0]['quote'] = 'evaluate models and report 99.2 accuracy.'
    with pytest.raises(ValueError):
        validate_points([point], blocks)


def test_valid_overview_can_cite_more_than_eight_source_blocks():
    blocks = [{'id': f'source-{i}', 'text': f'Figure {i}: Prompt template for task {i}.'} for i in range(10)]
    point = {'kind': '原文结论', 'text': '附录展示了各任务的提示模板。',
             'evidence': [{'id': b['id'], 'quote': b['text']} for b in blocks]}
    assert len(validate_points([point], blocks)[0]['evidence']) == 10


def test_long_exact_formula_quote_is_accepted():
    quote = 'Formula example: ' + 'P(x_i) + ' * 100
    point = {'kind': '原文结论', 'text': '对词元概率求和。',
             'evidence': [{'id': 'source-465', 'quote': quote}]}
    checked = validate_points([point], [{'id': 'source-465', 'text': quote}])
    assert checked[0]['evidence'][0]['quote'] == quote.strip()
    point['evidence'][0]['quote'] = quote + 'invented conclusion'
    with pytest.raises(ValueError):
        validate_points([point], [{'id': 'source-465', 'text': quote}])


def test_model_can_select_source_excerpt_without_retyping_formula(tmp_path):
    import httpx
    from paper_library.config import Settings, ProjectPaths
    settings = Settings.load(ProjectPaths.from_root(tmp_path), {
        'PAPER_TRANSLATION_PROFILE': 'test', 'LLM_TEST_MODEL': 'test',
        'LLM_TEST_BASE_URL': 'http://model/v1'})
    blocks = [{'id': 'source-465', 'text': 'The probability is $P(x) = P(x_1) + P(x_2)$.'}]
    def respond(request):
        payload = json.loads(json.loads(request.content)['messages'][1]['content'])
        excerpt = payload['source_excerpts'][0]
        value = {'role': '概率计算', 'points': [{'kind': '原文结论', 'text': '作者对概率求和。',
                 'evidence': [{'excerpt_id': excerpt['excerpt_id']}]}]}
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
            'message': {'content': json.dumps(value)}}]})
    service = GuideService(settings, transport=httpx.MockTransport(respond))
    result = service._ask({'task': '章节导读', 'blocks': blocks}, blocks)
    assert result['points'][0]['evidence'] == [{'id': 'source-465', 'quote': blocks[0]['text']}]


def test_cancelled_guide_does_not_call_model_or_publish(tmp_path):
    from paper_library.config import Settings, ProjectPaths
    settings = Settings.load(ProjectPaths.from_root(tmp_path), {
        'PAPER_TRANSLATION_PROFILE': 'test', 'LLM_TEST_MODEL': 'test', 'LLM_TEST_BASE_URL': 'http://model/v1'})
    path = tmp_path / 'article.md'
    path.write_text('# Title\n\nA paragraph with enough text.')
    with pytest.raises(ValueError, match='取消'):
        GuideService(settings).generate(path, cancelled=lambda: True, progress=lambda _: None)
    assert not path.with_name('reading-guide.json').exists()


def test_guide_uses_annotated_headings_and_preserves_unusual_sections():
    from paper_library.structure.document import build_blocks
    source='# Paper\n\nUnnumbered methods\n\nDetails here.\n\n# Results\n\nThe result.\n'
    roles=[{'id':b['id'],'role':'unknown'} for b in build_blocks(source)]
    roles[1].update(role='heading',level=2)
    roles[3].update(role='heading',level=2)
    doc=build_document(source,roles)
    assert [(s['title'],s['level']) for s in doc['sections']]==[('Paper',1),('Unnumbered methods',2),('Results',2)]
    assert doc['sections'][1]['blocks'][0]['text']=='Details here.'
    assert doc['sections'][1]['parent']==doc['sections'][0]['id']


def test_changed_structure_marks_guide_stale_even_when_source_is_unchanged(tmp_path):
    import hashlib,json
    from paper_library.structure.document import build_blocks,source_hash
    source='# Paper\n\nParagraph content long enough for an explanation.\n'
    (tmp_path/'article.md').write_text(source)
    point={'kind':'原文结论','text':'说明研究背景。','evidence':[
        {'id':'source-3','quote':'Paragraph content long enough for an explanation.'}]}
    section=build_document(source)['sections'][0]
    guide={'schema_version':1,'document_version':source_hash(source),'overview':[point],
           'sections':[{**section,'parts':[{'role':'背景','points':[point],'details':[],'skip_reason':''}]}],
           'structure_version':hashlib.sha256(json.dumps(None,sort_keys=True).encode()).hexdigest()}
    (tmp_path/'reading-guide.json').write_text(json.dumps(guide))
    assert not load_guide(tmp_path,source_hash(source))['stale']
    roles=[{'id':b['id'],'role':'body'} for b in build_blocks(source)]
    (tmp_path/'document-structure.json').write_text(json.dumps({'version':1,'source_sha256':source_hash(source),'annotations':roles}))
    assert load_guide(tmp_path,source_hash(source))['stale']


def test_overview_uses_main_text_without_appendix_examples():
    from paper_library.reading.service import _overview_source_sections

    sections = [
        {'title': 'Abstract', 'parts': [{'points': [{'text': 'abstract'}]}]},
        {'title': '1 Introduction', 'parts': [{'points': [{'text': 'introduction'}]}]},
        {'title': '6 Conclusion', 'parts': [{'points': [{'text': 'conclusion'}]}]},
        {'title': 'References', 'parts': [{'points': [{'text': 'citations'}]}]},
        {'title': 'A.1 Baselines', 'parts': [{'points': [{'text': 'appendix'}]}]},
        {'title': 'Example', 'parts': [{'points': [{'text': 'example'}]}]},
    ]
    assert [item['title'] for item in _overview_source_sections(sections)] == [
        'Abstract', '1 Introduction', '6 Conclusion'
    ]
    assert _overview_source_sections(sections[:3]) == sections[:3]
