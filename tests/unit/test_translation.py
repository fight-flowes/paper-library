from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from paper_library.web.markdown import render_markdown


def test_translation_slots_follow_blocks_and_protect_inline_content():
    source = "# A title\n\nFirst **bold** paragraph with $x^2$ and `code`.\n\nSecond paragraph.\n\n- List item\n\n![Image](images/a.jpg)\n\n```python\ncode()\n```\n\n$$x=1$$"
    rendered = render_markdown(source, "/asset", translation_slots=True)
    assert len(rendered.segments) == 4
    assert rendered.html.index('data-translation-id=') > rendered.html.index('</h1>')
    paragraph = rendered.segments[1]
    assert '$x^2$' not in paragraph.text and '`code`' not in paragraph.text
    translated = paragraph.text.replace('First', '第一').replace('paragraph with', '段落包含')
    output = paragraph.render_translation(translated)
    assert '<math' in output and '<code>code</code>' in output and '<strong>bold</strong>' in output
    assert '第二' not in output
    assert rendered.segments[2].id != paragraph.id
    assert rendered.segments == render_markdown(source, '/different/asset', translation_slots=True).segments


def test_translation_model_text_is_escaped_and_missing_placeholders_rejected():
    segment = render_markdown('Text $x$ end.', '/asset', translation_slots=True).segments[0]
    output = segment.render_translation(segment.text + '<script>bad()</script>')
    assert '<script>' not in output and '&lt;script&gt;' in output
    assert '符号缺失' in segment.render_translation('公式被删除')


def test_markdown_table_translations_remain_inside_cells():
    rendered = render_markdown('| Name | Value |\n| - | - |\n| Accuracy | High |', '/asset', translation_slots=True)
    assert len(rendered.segments) == 4
    assert rendered.html.index('data-translation-id=') < rendered.html.index('</th>')


def test_config_uses_only_selected_profile_without_exposing_secret(tmp_path, monkeypatch):
    from paper_library.translation.service import TranslationConfig

    env = tmp_path / '.env'
    env.write_text('LLM_API_KEY=wrong\nLLM_MODEL=wrong\nLLM_QWEN38_EXTERNAL_BASE_URL="http://localhost:9999/v1"\nLLM_QWEN38_EXTERNAL_MODEL=qwen3.8-27b\nLLM_QWEN38_EXTERNAL_API_KEY="private-value"\n')
    monkeypatch.setenv('PAPER_LIBRARY_SETTINGS_FILE', str(env))
    monkeypatch.setenv('PAPER_TRANSLATION_PROFILE', 'qwen3.8-27b')
    config = TranslationConfig.from_environment()
    assert config.model == 'qwen3.8-27b'
    assert config.base_url == 'http://localhost:9999/v1'
    assert config.api_key == 'private-value'
    assert 'private-value' not in repr(config)


def test_model_contract_and_cached_result(tmp_path):
    from paper_library.translation.service import TranslationConfig, TranslationService

    calls = []
    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert request.url.path == '/v1/chat/completions'
        assert request.headers['Authorization'] == 'Bearer secret'
        assert payload['chat_template_kwargs'] == {'enable_thinking': False}
        assert payload['model'] == 'qwen3.8-27b'
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': '中文译文'}}]})
    config = TranslationConfig('http://localhost/v1', 'qwen3.8-27b', 'secret', request_style='qwen')
    service = TranslationService(config, tmp_path / 'cache.sqlite3', transport=httpx.MockTransport(handler))
    segment = render_markdown('English paragraph.', '/asset', translation_slots=True).segments[0]
    assert service.translate('paper-a', 'version-a', segment) == '中文译文'
    assert service.translate('paper-a', 'version-a', segment) == '中文译文'
    assert len(calls) == 1
    assert service.cached('paper-b', 'version-a', segment) is None
    assert service.cached('paper-a', 'version-b', segment) == '中文译文'
    other = TranslationService(TranslationConfig('http://localhost/v1', 'other', 'secret'), tmp_path / 'cache.sqlite3')
    assert other.cached('paper-a', 'version-a', segment) is None


def test_cache_survives_unrelated_document_edits(tmp_path):
    from paper_library.translation.service import TranslationConfig, TranslationService

    calls = 0
    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': '目标译文'}}]})
    service = TranslationService(
        TranslationConfig('http://local/v1', 'model', ''),
        tmp_path / 'cache.sqlite3',
        transport=httpx.MockTransport(handler),
    )
    original = render_markdown('Earlier paragraph.\n\nTarget paragraph.', '/asset', translation_slots=True)
    target = original.segments[1]
    assert service.translate('paper', 'whole-document-v1', target) == '目标译文'
    edited = render_markdown('New paragraph.\n\nEarlier paragraph.\n\nTarget paragraph.', '/asset', translation_slots=True)
    assert edited.segments[2].id != target.id
    assert service.cached('paper', 'whole-document-v2', edited.segments[2]) == '目标译文'
    assert calls == 1


def test_cache_isolated_by_prompt_and_request_style(tmp_path, monkeypatch):
    import paper_library.translation.service as translation
    from paper_library.translation.service import TranslationConfig, TranslationService

    segment = render_markdown('English paragraph.', '/asset', translation_slots=True).segments[0]
    cache = tmp_path / 'cache.sqlite3'
    response = httpx.MockTransport(lambda _r: httpx.Response(200, json={
        'choices': [{'finish_reason': 'stop', 'message': {'content': '中文'}}]}))
    qwen = TranslationService(
        TranslationConfig('http://local/v1', 'same-model', '', request_style='qwen'),
        cache,
        transport=response,
    )
    qwen.translate('paper', 'v1', segment)
    openai = TranslationService(
        TranslationConfig('http://local/v1', 'same-model', '', request_style='openai'),
        cache,
    )
    assert openai.cached('paper', 'v1', segment) is None
    monkeypatch.setattr(translation, 'SYSTEM_PROMPT', translation.SYSTEM_PROMPT + '\n新协议')
    changed_prompt = TranslationService(qwen.config, cache)
    assert changed_prompt.cached('paper', 'v1', segment) is None


@pytest.mark.parametrize('response', [
    {'choices': [{'finish_reason': 'stop', 'message': {'content': ''}}]},
    {'choices': [{'finish_reason': 'stop', 'message': {'content': '<think>推理</think>'}}]},
])
def test_invalid_model_results_are_not_cached(tmp_path, response):
    from paper_library.translation.service import TranslationConfig, TranslationService, TranslationError

    service = TranslationService(TranslationConfig('http://localhost/v1', 'model', ''), tmp_path / 'cache.sqlite3', transport=httpx.MockTransport(lambda r: httpx.Response(200, json=response)))
    segment = render_markdown('English $x$ paragraph.', '/asset', translation_slots=True).segments[0]
    with pytest.raises(TranslationError):
        service.translate('paper', 'version', segment)
    assert service.cached('paper', 'version', segment) is None


def test_unavailable_model_returns_sanitized_error(tmp_path):
    from paper_library.translation.service import TranslationConfig, TranslationService, TranslationError

    def unavailable(request):
        raise httpx.ConnectError('sensitive URL and credential', request=request)
    service = TranslationService(TranslationConfig('http://localhost/v1', 'model', 'secret'), tmp_path / 'cache.sqlite3', transport=httpx.MockTransport(unavailable))
    segment = render_markdown('Paragraph.', '/asset', translation_slots=True).segments[0]
    with pytest.raises(TranslationError) as error:
        service.translate('paper', 'version', segment)
    assert error.value.status == 503
    assert 'sensitive' not in str(error.value) and 'secret' not in str(error.value)


def test_invalid_optional_translation_url_does_not_break_reader(monkeypatch, tmp_path):
    from paper_library.config import ProjectPaths, Settings
    from paper_library.web.app import create_app
    monkeypatch.setenv('PAPER_LIBRARY_SETTINGS_FILE', str(tmp_path / 'missing.env'))
    monkeypatch.setenv('PAPER_TRANSLATION_BASE_URL', 'http://[broken')
    paths = ProjectPaths.from_root(tmp_path)
    paths.libraries.mkdir()
    app = create_app(Settings.load(paths))
    assert app.test_client().get('/').status_code == 200
    assert not app.extensions['translation_service'].config.configured


def test_optional_config_is_disabled_when_settings_are_missing(tmp_path, monkeypatch):
    from paper_library.translation.service import TranslationConfig

    monkeypatch.setenv('PAPER_LIBRARY_SETTINGS_FILE', str(tmp_path / 'missing.env'))
    monkeypatch.delenv('PAPER_TRANSLATION_PROFILE', raising=False)
    config = TranslationConfig.from_environment(required=False)
    assert not config.configured
    assert config.profile == ''


def test_numeric_quantities_and_units_are_protected():
    source = 'We release 2.8k poems from 19k examples, scoring 95.2% in 10 ms with Qwen3.8-27B.'
    segment = render_markdown(source, '/asset', translation_slots=True).segments[0]
    assert '2.8k' not in segment.text
    assert '19k' not in segment.text
    assert '95.2%' not in segment.text
    translated = '中文 ' + ''.join(marker for marker, _ in segment.protected)
    html = segment.render_translation(translated)
    for value in ('2.8k', '19k', '95.2%', '10 ms', 'Qwen3.8-27B'):
        assert value in html
    assert segment.render_translation(translated + ' 2.8').endswith(' 2.8')


def test_affiliation_numbers_do_not_hide_institution_names_from_model():
    source = '1Huazhong University of Science and Technology, 2Lehigh University'
    segment = render_markdown(source, '/asset', translation_slots=True).segments[0]
    assert [fragment for _, fragment in segment.protected] == ['1', '2']
    assert 'Huazhong University of Science and Technology' in segment.text
    assert 'Lehigh University' in segment.text
    first, second = (marker for marker, _ in segment.protected)
    assert segment.render_translation(f'{first}华中科技大学，{second}理海大学') == '1华中科技大学，2理海大学'


def test_reading_missing_cache_does_not_create_it(tmp_path):
    from paper_library.translation.service import TranslationConfig, TranslationService
    cache = tmp_path / 'absent/cache.sqlite3'
    service = TranslationService(TranslationConfig('', '', ''), cache)
    segment = render_markdown('Text.', '/asset', translation_slots=True).segments[0]
    assert service.cached('paper', 'version', segment) is None
    assert not cache.parent.exists()


def test_service_allows_bounded_parallel_model_calls(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Lock
    from paper_library.translation.service import TranslationConfig, TranslationService
    barrier = Barrier(3)
    lock = Lock()
    active = 0
    peak = 0
    def handler(request):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        barrier.wait(timeout=5)
        with lock:
            active -= 1
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': '译文'}}]})
    service = TranslationService(TranslationConfig('http://local/v1', 'qwen', ''), tmp_path / 'cache.sqlite3', max_concurrency=3, transport=httpx.MockTransport(handler))
    segment = render_markdown('Text.', '/asset', translation_slots=True).segments[0]
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda paper: service.translate(paper, 'version', segment), ['a','b','c']))
    assert results == ['译文'] * 3
    assert peak == 3


@pytest.mark.parametrize('value', ['1920x1080', '5HR53zXJIPAC'])
def test_mixed_numeric_identifiers_are_fully_protected(value):
    from paper_library.translation.segments import _MARKER
    segment = render_markdown('Example ' + value, '/asset', translation_slots=True).segments[0]
    assert not any(char.isdigit() for char in _MARKER.sub('', segment.text))
    assert value in segment.render_translation(segment.text)


def test_numbered_heading_does_not_require_model_to_copy_its_number(tmp_path):
    from paper_library.translation.service import TranslationConfig, TranslationService
    segment = render_markdown('# 1 Introduction', '/asset', translation_slots=True).segments[0]
    service = TranslationService(TranslationConfig('http://local/v1', 'model', ''), tmp_path/'cache.sqlite3', transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'choices':[{'finish_reason':'stop','message':{'content':'引言'}}]})))
    result = service.translate('paper','version',segment)
    assert segment.render_translation(result) == '1 引言'


def test_model_receives_quoted_text_with_short_placeholders(tmp_path):
    from paper_library.translation.service import TranslationConfig, TranslationService
    segment = render_markdown('We release 2.8k poems.', '/asset', translation_slots=True).segments[0]
    def handler(request):
        body = json.loads(request.content)
        quoted = json.loads(body['messages'][-1]['content'])['source_text']
        assert quoted == 'We release ⟦PH_0⟧ poems.'
        return httpx.Response(200, json={'choices':[{'finish_reason':'stop','message':{'content':'我们发布 ⟦PH_0⟧ 首诗。'}}]})
    service = TranslationService(TranslationConfig('http://local/v1','model',''), tmp_path/'cache.sqlite3', transport=httpx.MockTransport(handler))
    translated = service.translate('paper','version',segment)
    assert segment.render_translation(translated) == '我们发布 2.8k 首诗。'


def test_missing_numeric_markers_are_repaired_before_caching(tmp_path):
    from paper_library.translation.service import TranslationConfig, TranslationService
    calls = []
    def handler(request):
        data = json.loads(json.loads(request.content)['messages'][-1]['content'])
        calls.append(data)
        if len(calls) == 1:
            text = 'WildChat：ChatGPT 交互日志。'
        else:
            assert data['required_markers'] == ['⟦PH_0⟧', '⟦PH_1⟧']
            assert data['validation_errors']
            assert data['previous_translation'] == 'WildChat：ChatGPT 交互日志。'
            text = 'WildChat：⟦PH_0⟧ 条 ChatGPT 交互日志，载于第 ⟦PH_1⟧ 届会议。'
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': text}}]})
    service = TranslationService(TranslationConfig('http://local/v1', 'model', ''), tmp_path/'cache.sqlite3',
                                 transport=httpx.MockTransport(handler))
    segment = render_markdown('WildChat: 1M ChatGPT interaction logs. In 12th conference.', '/asset', translation_slots=True).segments[0]
    result = service.translate('paper', 'version', segment)
    assert len(calls) == 2
    assert segment.integrity_warnings(result) == []
    assert '1M' in segment.render_translation(result) and '12th' in segment.render_translation(result)
    assert service.cached_record('paper', 'version', segment).warnings == ()


def test_failed_repair_is_bounded_and_never_replaces_a_better_saved_translation(tmp_path):
    from paper_library.translation.service import TranslationConfig, TranslationService
    calls = []
    def handler(request):
        calls.append(request)
        content = '中文 ⟦PH_0⟧ ⟦PH_1⟧' if len(calls) == 1 else '标记全部丢失'
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]})
    service = TranslationService(TranslationConfig('http://local/v1', 'model', ''), tmp_path/'cache.sqlite3',
                                 transport=httpx.MockTransport(handler))
    segment = render_markdown('We use 10 examples and 20 controls.', '/asset', translation_slots=True).segments[0]
    original = service.translate('paper', 'version', segment)
    assert service.translate('paper', 'version', segment, force=True) == original
    assert len(calls) == 4
    assert service.cached('paper', 'version', segment) == original


def test_damaged_marker_is_detected_and_not_exposed_in_reader():
    segment = render_markdown('Index S-84 and S-87.', '/asset', translation_slots=True).segments[0]
    damaged = '目录 S-⟦PH_0 和 S-⟦PH_1⟧'
    assert segment.integrity_warnings(damaged)
    assert '⟦PH_' not in segment.render_translation(damaged)


def test_superscript_footnote_numbers_are_protected():
    segment = render_markdown('An annotation¹ and another² example.', '/asset', translation_slots=True).segments[0]
    assert '¹' not in segment.text and '²' not in segment.text
    output = segment.render_translation(segment.text)
    assert '¹' in output and '²' in output


def test_numeric_markers_can_follow_chinese_word_order_but_not_be_lost_or_duplicated():
    segment = render_markdown('We gathered 12k poems with Tool (2024).', '/asset', translation_slots=True).segments[0]
    a, b = [marker for marker, _ in segment.protected]
    translated = f'我们使用 Tool（{b}）收集了 {a} 首诗。'
    assert segment.render_translation(translated) == '我们使用 Tool（2024）收集了 12k 首诗。'
    assert '重复' in segment.render_translation(f'{a}{a}{b}')
    assert '符号缺失' in segment.render_translation(a)
    styled = render_markdown('**Bold** text.', '/asset', translation_slots=True).segments[0]
    first, last = [marker for marker, _ in styled.protected]
    assert '<strong>' not in styled.render_translation(last + '文本' + first)


def test_semantic_edge_numbers_remain_in_model_context(tmp_path):
    from paper_library.translation.service import TranslationConfig, TranslationService
    segment = render_markdown('We evaluate accuracy using GPT-4', '/asset', translation_slots=True).segments[0]
    def handler(request):
        source = json.loads(json.loads(request.content)['messages'][-1]['content'])['source_text']
        assert '⟦PH_0⟧' in source
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'使用 GPT-⟦PH_0⟧ 评估准确率'}}]})
    service = TranslationService(TranslationConfig('http://local/v1','model',''),tmp_path/'cache.sqlite3',transport=httpx.MockTransport(handler))
    result = service.translate('paper','version',segment)
    assert segment.render_translation(result) == '使用 GPT-4 评估准确率'


def test_caption_translation_keeps_image_only_in_original():
    rendered = render_markdown('![](images/figure.jpg)  \nFigure 10: Example caption.', '/asset', translation_slots=True)
    segment = rendered.segments[0]
    assert all('<img' not in fragment and '<br' not in fragment for _,fragment in segment.protected)
    translated = segment.render_translation(segment.text.replace('Figure','图').replace('Example caption.','示例图注。'))
    assert '<img' not in translated
    assert rendered.html.count('<img') == 1


def test_hardbreak_translation_renders_one_break_without_extra_model_whitespace():
    segment = render_markdown('• First item.  \n• Second item.', '/asset', translation_slots=True).segments[0]
    marker = segment.protected[0][0]
    translated = f'• 第一项。\n{marker}\n\n• 第二项。'
    assert segment.render_translation(translated) == '• 第一项。<br />• 第二项。'


def test_translation_preserves_whitespace_away_from_hardbreaks():
    segment = render_markdown('First line\nSecond line.', '/asset', translation_slots=True).segments[0]
    assert segment.render_translation('第一行\n  第二行') == '第一行\n  第二行'


def test_formula_markers_can_move_but_must_remain_exactly_once():
    segment = render_markdown('Track $P(C)$ by summing $w(C)$.', '/asset', translation_slots=True).segments[0]
    a, b = [marker for marker, _ in segment.protected]
    output = segment.render_translation(f'通过求和 {b} 来跟踪 {a}。')
    assert output.count('<math ') == 2
    assert '遗漏' in segment.render_translation(f'仅有 {a}')
    assert '重复' in segment.render_translation(f'{a}{b}{b}')


def test_formula_reordering_does_not_allow_reversed_format_tags():
    segment = render_markdown('**Track** $P(C)$ using $w(C)$.', '/asset', translation_slots=True).segments[0]
    opening, closing, a, b = [marker for marker, _ in segment.protected]
    assert '<strong>' not in segment.render_translation(f'{closing}跟踪{opening}{b}{a}')


def test_readable_translation_with_generated_digits_is_accepted():
    segment = render_markdown('On July first, evaluate $x$ with 12 samples.', '/asset', translation_slots=True).segments[0]
    formula, count = [marker for marker, _ in segment.protected]
    result = segment.render_translation(f'7月1日，使用 {count} 个样本评估 {formula}。')
    assert '7月1日' in result and '12 个样本' in result and '<math ' in result
    assert '遗漏' in segment.render_translation('7月1日，使用12个样本评估x。')


def test_unknown_short_placeholder_is_shown_as_missing_symbol():
    segment = render_markdown('Where $x$ is a sample.', '/asset', translation_slots=True).segments[0]
    marker = segment.protected[0][0]
    output = segment.render_translation(f'其中 {marker} 是样本，⟦PH_1⟧ 为集合。')
    assert '〔符号缺失〕' in output and '⟦PH_' not in output
    assert '<math ' in output


@pytest.mark.parametrize('content,reason,expected', [
    ('部分译文', 'length', '译文未完整生成'),
    ('<think>推理</think>正文', 'stop', '正文'),
    ('```text\n译文\n```', 'stop', '译文'),
    ('公式丢失但仍可阅读', 'stop', '符号缺失'),
])
def test_usable_imperfect_responses_are_cached(tmp_path, content, reason, expected):
    from paper_library.translation.service import TranslationConfig, TranslationService
    service = TranslationService(TranslationConfig('http://local/v1', 'model', ''), tmp_path/'cache.sqlite3',
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'choices': [
            {'finish_reason': reason, 'message': {'content': content}}]})))
    segment = render_markdown('Text $x$.', '/asset', translation_slots=True).segments[0]
    result = service.translate('paper', 'version', segment)
    assert service.cached('paper', 'version', segment) == result
    record = service.cached_record('paper', 'version', segment)
    assert record is not None
    assert record.text == result
    assert record.model == 'model'
    assert record.prompt_fingerprint == service.prompt_fingerprint
    assert record.request_style == 'openai'
    assert record.finish_reason == reason
    assert record.created_at
    assert record.warnings == tuple(segment.warnings(result))
    assert expected in segment.render_translation(result)
    assert '<think>' not in result and '```' not in result


def test_prose_html_tag_names_are_literal_in_original_and_translation():
    source = 'Use <div> elements, paragraphs with <br> line breaks, and <p> tags.'
    rendered = render_markdown(source, '/asset', translation_slots=True)
    segment = rendered.segments[0]
    for output in (rendered.html, segment.render_translation(segment.text)):
        for tag in ('div', 'br', 'p'):
            assert f'<code>&lt;{tag}&gt;</code>' in output
        assert '<br>' not in output
    normal = render_markdown('First<br>Second', '/asset', translation_slots=True)
    assert '<br>' in normal.html
    assert '<br>' in normal.segments[0].render_translation(normal.segments[0].text)


def test_deepseek_profile_config_payload_and_cache_isolation(tmp_path, monkeypatch):
    from paper_library.translation.service import TranslationConfig, TranslationService
    env = tmp_path/'models.env'
    env.write_text('LLM_QWEN38_EXTERNAL_BASE_URL=http://qwen/v1\nLLM_QWEN38_EXTERNAL_MODEL=qwen3.8-27b\nLLM_QWEN38_EXTERNAL_MAX_CONCURRENCY=32\nLLM_DEEPSEEK_DEV_BASE_URL=http://deepseek/v1\nLLM_DEEPSEEK_DEV_MODEL=deepseek-v4-flash\nLLM_DEEPSEEK_DEV_API_KEY=test-secret\nLLM_DEEPSEEK_DEV_MAX_CONCURRENCY=2\nLLM_DEEPSEEK_DEV_REQUEST_STYLE=deepseek\n')
    monkeypatch.setenv('PAPER_LIBRARY_SETTINGS_FILE', str(env))
    monkeypatch.setenv('PAPER_TRANSLATION_PROFILE', 'qwen3.8-27b')
    qwen = TranslationConfig.from_environment()
    deepseek = TranslationConfig.from_environment('deepseek-v4-flash')
    assert qwen.concurrency == 32 and qwen.profile == 'qwen3.8-27b'
    assert deepseek.concurrency == 2 and deepseek.model == 'deepseek-v4-flash'
    def handler(request):
        payload = json.loads(request.content)
        assert payload['thinking'] == {'type': 'disabled'}
        assert 'chat_template_kwargs' not in payload
        assert request.headers['Authorization'] == 'Bearer test-secret'
        return httpx.Response(200, json={'choices':[{'finish_reason':'stop','message':{'content':'译文'}}]})
    service = TranslationService(deepseek, tmp_path/'cache.sqlite3', transport=httpx.MockTransport(handler))
    segment = render_markdown('English text.', '/asset', translation_slots=True).segments[0]
    assert service.translate('paper','version',segment) == '译文'
    assert TranslationService(qwen,service.cache_path).cached('paper','version',segment) is None


def test_project_dotenv_selects_default_model_and_concurrency(tmp_path, monkeypatch):
    from paper_library.translation.service import TranslationConfig
    upstream = tmp_path/'upstream.env'
    upstream.write_text('LLM_DEEPSEEK_DEV_BASE_URL=http://deepseek/v1\nLLM_DEEPSEEK_DEV_MODEL=deepseek-v4-flash\nLLM_DEEPSEEK_DEV_MAX_CONCURRENCY=2\nLLM_DEEPSEEK_DEV_REQUEST_STYLE=deepseek\n')
    local = tmp_path/'local.env'
    local.write_text(upstream.read_text() + 'PAPER_TRANSLATION_PROFILE=deepseek-v4-flash\nLLM_DEEPSEEK_DEV_MAX_CONCURRENCY=16\nLLM_QWEN_MODEL=qwen3.8-27b\n')
    monkeypatch.setenv('PAPER_LIBRARY_SETTINGS_FILE', str(local))
    monkeypatch.delenv('PAPER_TRANSLATION_ENV_FILE', raising=False)
    monkeypatch.delenv('PAPER_TRANSLATION_PROFILE', raising=False)
    config = TranslationConfig.from_environment()
    assert config.profile == 'deepseek-v4-flash' and config.concurrency == 16
    assert config.model == 'deepseek-v4-flash'
    assert TranslationConfig.from_environment('qwen3.8-27b').profile == 'qwen3.8-27b'
    monkeypatch.setenv('PAPER_TRANSLATION_MAX_CONCURRENCY', '4')
    assert TranslationConfig.from_environment().concurrency == 4

@pytest.mark.parametrize('word,refs', [('reliability','1,2'), ('use','3'), ('verifcation','4'), ('feedback','5'), ('models','6,7'), ('theory','8,9'), ('abstention','10,11'), ('leaderboards','12')])
def test_prose_glued_to_citation_remains_translatable(word, refs):
    segment = render_markdown(f'Previous work improves {word}{refs}.', '/asset', translation_slots=True).segments[0]
    assert word in segment.text
    assert [fragment for _, fragment in segment.protected] == [refs]

@pytest.mark.parametrize('value', ['Qwen3.8-27B', 'BERT2', 'LLaMA2', 'x1', '1920x1080'])
def test_citation_split_preserves_model_and_variable_identifiers(value):
    segment = render_markdown('Example ' + value, '/asset', translation_slots=True).segments[0]
    assert value in [fragment for _, fragment in segment.protected]

def test_untitled_abstract_ends_frontmatter_styling():
    abstract = ('Large language models sometimes produce plausible falsehoods. '
                'Previous work has offered numerous explanations and effective mitigations. '
                'Nonetheless, the problem persists even in state-of-the-art language models.')
    source = '# Paper title\n\nReceived: 1 July 2025\n\nAdam Kalai & Ofir Nachum\n\n' + abstract + '\n\nShort body paragraph.\n\n## Results\n\nResults here.'
    result = render_markdown(source, '/asset', translation_slots=True)
    from bs4 import BeautifulSoup
    paragraphs = BeautifulSoup(result.html, 'html.parser').select('.paper-frontmatter')
    assert [p.get_text() for p in paragraphs] == ['Received: 1 July 2025', 'Adam Kalai & Ofir Nachum']


def test_distinct_service_instances_can_migrate_the_same_cache(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from paper_library.translation.service import TranslationConfig,TranslationService
    gate=Barrier(4)
    def connect(_):
        service=TranslationService(TranslationConfig('http://model','test',''),tmp_path/'shared.sqlite3')
        gate.wait(5)
        with service._connect() as connection:
            columns={row[1] for row in connection.execute('PRAGMA table_info(translations)')}
            assert 'generation' in columns
        connection.close()
    with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(connect,range(4)))


@pytest.mark.parametrize('token', ['<|INST>', '<|im_start|>'])
def test_chat_control_tokens_are_protected_without_hiding_surrounding_prose(token):
    source = f'output: {token} Hello, how are you?'
    segment = render_markdown(source, '/asset', translation_slots=True).segments[0]
    assert len(segment.protected) == 1
    assert token not in segment.text
    assert 'Hello, how are you?' in segment.text
    marker, fragment = segment.protected[0]
    assert fragment == token.replace('<', '&lt;').replace('>', '&gt;')
    assert segment.render_translation(f'输出：{marker} 你好，你怎么样？') == (
        f'输出：{fragment} 你好，你怎么样？'
    )
