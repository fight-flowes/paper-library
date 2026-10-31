import json

import httpx
import pytest

from paper_library.config import Settings, ProjectPaths
from paper_library.ingest.paragraphs import repair
from paper_library.reading.service import GuideService
from paper_library.translation.service import TranslationConfig, TranslationService
from paper_library.web.markdown import render_translatable_markdown


def configured(tmp_path, temperature):
    paths = ProjectPaths.from_root(tmp_path)
    paths.dotenv.write_text(f'LLM_TEMPERATURE={temperature}\n')
    return Settings.load(paths, {'PAPER_TRANSLATION_PROFILE': 'test',
        'LLM_TEST_MODEL': 'test', 'LLM_TEST_BASE_URL': 'http://model/v1'})


@pytest.mark.parametrize('temperature', ['0.1', '0.35', '0', '2'])
def test_every_llm_payload_uses_shared_dotenv_temperature(tmp_path, temperature):
    settings = configured(tmp_path, temperature)
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload['temperature'])
        system = payload['messages'][0]['content']
        if '两个相邻正文段落' in system:
            value = json.dumps({'decision': 'keep', 'reason': '独立段落'})
        elif '论文阅读助手' in system:
            value = json.dumps({'role': '', 'points': [], 'details': [], 'skip_reason': '无内容'})
        else:
            value = '中文译文'
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': value}}]})

    transport = httpx.MockTransport(handler)
    config = TranslationConfig.from_settings(settings.translation())
    segment = render_translatable_markdown('English paragraph.', '/asset').segments[0]
    TranslationService(config, tmp_path / 'cache.sqlite3', transport=transport).translate('paper', 'version', segment)
    source = ('This is a sufficiently long academic paragraph describing the context of a study and demonstrating\n\n'
              'that the next sufficiently long paragraph may be related to the first one and should be examined.')
    repair(source, settings=settings, transport=transport)
    GuideService(settings, transport=transport)._ask({'task': '章节导读'}, [
        {'id': 'source-1', 'text': 'A paragraph with enough evidence to summarize.'}])
    assert calls == [float(temperature)] * 3


@pytest.mark.parametrize('value', ['nan', 'inf', '-1', '2.1', 'invalid'])
def test_invalid_temperature_is_rejected(tmp_path, value):
    with pytest.raises(ValueError):
        configured(tmp_path, value).translation()
