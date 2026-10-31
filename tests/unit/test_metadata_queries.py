import json

import httpx

from paper_library.config import ProjectPaths, Settings, TranslationSettings


def test_query_planner_uses_local_context_and_model_settings(tmp_path):
    from paper_library.metadata.queries import generate_search_plan
    settings = Settings.load(ProjectPaths.from_root(tmp_path), {})
    config = TranslationSettings('http://model/v1', 'qwen', '', temperature=.1, request_style='qwen')
    class Configured:
        values = settings.values
        def translation(self, required=False):
            return config
    def respond(request):
        data = json.loads(request.content)
        assert data['temperature'] == .1
        assert 'ICLR 2024' in data['messages'][1]['content']
        assert 'NeurIPS-2025' not in data['messages'][1]['content']
        assert 'Wrong Document' not in data['messages'][1]['content']
        assert 'Jane Doe' in data['messages'][1]['content']
        assert data['chat_template_kwargs']['enable_thinking'] is False
        return httpx.Response(200, json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({
            'primary':'Prompt Formatting ICLR 2024 Jane Doe',
            'alternatives':['Prompt Formatting Jane Doe', 'Prompt Formatting Jane Doe', 'Prompt Formatting EMNLP 2023']})}}]})
    result = generate_search_plan([('markdown','Prompt Formatting'), ('filename','Wrong Document'), ('catalog','Wrong Document')],
        'NeurIPS-2025-unrelated.pdf', '# Prompt Formatting\nJane Doe\nPublished at ICLR 2024', '2025', 'NeurIPS',
        settings=Configured(), transport=httpx.MockTransport(respond))
    assert result['primary'] == 'Prompt Formatting ICLR 2024 Jane Doe'
    assert result['alternatives'] == ['Prompt Formatting Jane Doe']
    assert result['status'] == 'generated'


def test_parsed_frontmatter_is_preferred_to_embedded_pdf_title(monkeypatch):
    from paper_library.metadata.repair import verify_bibliography
    from paper_library.metadata.service import GatewayResponse
    monkeypatch.setattr('paper_library.metadata.repair.pdf_frontmatter', lambda path:('Unrelated Internal Document Title','Unrelated metadata text'))
    seen = []
    def plan(clues, filename, front, *args, **kwargs):
        assert clues[0] == ('markdown', 'Actual Paper Title')
        assert 'Jane Doe' in front
        return {'status':'unconfigured'}
    monkeypatch.setattr('paper_library.metadata.queries.generate_search_plan', plan)
    class Gateway:
        def ready(self): return GatewayResponse(200,{})
        def search(self, query):
            seen.append(query)
            return GatewayResponse(200,{'results':[]})
    verify_bibliography('', '', '', 'download.pdf', '# Actual Paper Title\nJane Doe\n\n# Abstract\nBody', Gateway(), pdf='dummy.pdf')
    assert seen[0] == '"Actual Paper Title"'


def test_filename_remains_available_without_parsed_text(tmp_path):
    from paper_library.metadata.queries import generate_search_plan
    class Configured:
        values = {}
        def translation(self, required=False):
            return TranslationSettings('http://model/v1','qwen','')
    def respond(request):
        payload = json.loads(json.loads(request.content)['messages'][1]['content'])
        assert payload['fallback_metadata']['original_filename'] == 'Actual-Paper.pdf'
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':
            '{"primary":"Actual Paper","alternatives":[]}'}}]})
    result = generate_search_plan([('filename','Actual Paper')], 'Actual-Paper.pdf','','','', settings=Configured(),transport=httpx.MockTransport(respond))
    assert result['primary'] == 'Actual Paper'


def test_query_planner_model_failure_falls_back(tmp_path):
    from paper_library.metadata.queries import generate_search_plan
    class Configured:
        values = {}
        def translation(self, required=False):
            return TranslationSettings('http://model/v1','qwen','')
    result = generate_search_plan([('markdown','Prompt Formatting')], '', '', '', '', settings=Configured(),
        transport=httpx.MockTransport(lambda request:httpx.Response(503)))
    assert result['status'] == 'fallback'
    assert not result.get('primary')


def test_queries_do_not_replace_identity_or_stop_at_preprint(monkeypatch):
    from paper_library.metadata.repair import verify_bibliography
    from paper_library.metadata.service import GatewayResponse
    title = 'A Paper About Prompt Formatting'
    preprint = 'https://arxiv.org/abs/2310.11324'
    official = 'https://aclanthology.org/2024.acl-long.1/'
    monkeypatch.setattr('paper_library.metadata.queries.generate_search_plan', lambda *a, **kw: {
        'status':'generated','primary':'prompt formatting Doe', 'alternatives':['prompt formatting Doe conference']})
    calls = []
    class Gateway:
        def ready(self): return GatewayResponse(200,{})
        def search(self, query):
            calls.append(query)
            return GatewayResponse(200,{'results':[{'title':title,'url': official if 'conference' in query else preprint}]})
        def extract(self, urls):
            content = f'# {title}\nAuthors: Jane Doe\nVenue: ACL\nPublication date: 2024-01-01'
            if urls[0] == preprint: content = f'# Title:{title}'
            return GatewayResponse(200,{'results':[{'url':urls[0],'raw_content':content}]})
    result = verify_bibliography(title,'','','paper.pdf',f'# {title}',Gateway())
    assert result.status == 'verified'
    assert result.bibliography['official_url'] == official
    assert calls == ['prompt formatting Doe','prompt formatting Doe conference']
    assert len(result.enrichment['attempts']) == 2
