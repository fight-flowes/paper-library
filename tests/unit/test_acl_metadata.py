import httpx
import pytest

from paper_library.metadata.identifiers import ACLClient
from paper_library.metadata.repair import verify_bibliography, parsed_display_title
from paper_library.metadata.service import GatewayResponse, _extract_acl_bibliography

TITLE = 'A Clear Paper About Entity Description'
ID = '2025.emnlp-main.1'
URL = f'https://aclanthology.org/{ID}/'
HTML = f'''<html><head>
<meta name="citation_title" content="{TITLE}">
<meta name="citation_author" content="Jane Doe">
<meta name="citation_author" content="\u202aJosé Roe">
<meta name="citation_conference_title" content="Proceedings of EMNLP 2025">
<meta name="citation_publication_date" content="2025/11">
<meta name="citation_doi" content="10.18653/v1/{ID}">
<meta name="citation_pdf_url" content="https://aclanthology.org/{ID}.pdf">
</head><body><h6>Details</h6><h2>Other interface text</h2></body></html>'''
MARKDOWN = f'''![ACL Logo](/logo.svg)
###### Details
## [{TITLE}](https://aclanthology.org/{ID}.pdf)
[Jane Doe](/people/jane-doe/),
[José Roe](https://aclanthology.org/people/jose-roe/)
##### Correct Metadata for
[An Editor](/people/editor/)
##### Abstract
##### Export citation
'''


def client(content=HTML, status=200):
    return ACLClient(transport=httpx.MockTransport(lambda request:httpx.Response(status,text=content)))


class Gateway:
    def __init__(self, content=MARKDOWN):
        self.content = content
        self.extracted = []
        self.searched = []

    def ready(self):
        return GatewayResponse(200, {})

    def extract(self, urls):
        self.extracted.extend(urls)
        return GatewayResponse(200, {'results':[{'url':urls[0], 'raw_content':self.content}]})

    def search(self, query):
        self.searched.append(query)
        return GatewayResponse(200, {'results':[]})


def verify(monkeypatch, gateway, lookup=None, title=TITLE):
    monkeypatch.setattr('paper_library.metadata.queries.generate_search_plan',lambda *a,**k:{'status':'disabled'})
    return verify_bibliography(ID,'','',ID+'.pdf','# '+title+'\n\nJane Doe',gateway,acl_lookup=lookup)


def test_citation_metadata_ignores_navigation_and_reads_all_fields():
    result=client().lookup(URL)
    assert result.status=='verified'
    assert result.bibliography=={
        'canonical_title':TITLE,'acl_anthology_id':ID,'official_url':URL,
        'venue_type':'conference','authors':['Jane Doe','José Roe'],
        'venue':'Proceedings of EMNLP 2025','publication_date':'2025-11',
        'doi':'10.18653/v1/'+ID,'official_pdf_url':URL.rstrip('/')+'.pdf',
    }
    assert result.enrichment['title_evidence']=='citation_title'


@pytest.mark.parametrize('content',[
    '<h6>Details</h6><h2>Abstract</h2>',
    HTML+f'<meta name="citation_title" content="A Different Paper">',
    HTML.replace(ID+'.pdf','2025.emnlp-main.2.pdf'),
    HTML.replace('10.18653/v1/'+ID,'10.18653/v1/2025.emnlp-main.2'),
])
def test_incomplete_or_internally_inconsistent_page_is_not_a_paper_conflict(content):
    result=client(content).lookup(URL)
    assert result.status=='ambiguous'
    assert result.enrichment['reason']=='extraction_incomplete'
    assert result.bibliography=={}


def test_acl_client_restricts_destinations_and_does_not_follow_redirects():
    calls=[]
    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(302,headers={'Location':'http://127.0.0.1/private'})
    lookup=ACLClient(transport=httpx.MockTransport(respond))
    assert lookup.lookup('http://127.0.0.1/private').status=='not_found'
    assert lookup.lookup(URL).status=='unavailable'
    assert calls==[URL]


def test_markdown_fallback_finds_paper_heading_and_multiline_authors():
    result=_extract_acl_bibliography('Do not trust this fallback',URL,MARKDOWN)
    assert result['canonical_title']==TITLE
    assert result['authors']==['Jane Doe','José Roe']
    assert 'An Editor' not in result['authors']


@pytest.mark.parametrize('content',[
    '###### Details\n##### Abstract\n##### Export citation',
    '# First Possible Title\n# Second Possible Title',
    f'## [A Different Paper](https://aclanthology.org/2025.emnlp-main.2.pdf)',
])
def test_unusable_markdown_title_is_not_replaced_with_the_local_title(content):
    assert _extract_acl_bibliography(TITLE,URL,content)=={}


def test_direct_metadata_lookup_verifies_without_tavily(monkeypatch):
    gateway=Gateway()
    result=verify(monkeypatch,gateway,client())
    assert result.status=='verified'
    assert result.bibliography['doi']=='10.18653/v1/'+ID
    assert not gateway.searched and not gateway.extracted
    assert result.enrichment['attempts'][0]['origin']=='acl-metadata'
    assert result.enrichment['matched_clues']==['markdown']


def test_direct_lookup_outage_falls_back_to_verified_markdown(monkeypatch):
    gateway=Gateway()
    result=verify(monkeypatch,gateway,client(status=503))
    assert result.status=='verified'
    assert result.bibliography['canonical_title']==TITLE
    assert result.enrichment['attempts'][0]['status']=='unavailable'
    assert gateway.extracted==[URL]


def test_navigation_only_extract_is_not_reported_as_a_conflict(monkeypatch):
    result=verify(monkeypatch,Gateway('###### Details\n##### Abstract'))
    assert result.status=='ambiguous'
    assert result.enrichment['attempts'][0]['status']=='ambiguous'


def test_a_real_paper_title_mismatch_still_blocks_updates(monkeypatch):
    result=verify(monkeypatch,Gateway(),client(),title='A Completely Different Research Paper')
    assert result.status=='conflict'
    assert result.bibliography=={}
    assert result.enrichment['candidates'][0]['title']==TITLE


@pytest.mark.parametrize('title,heading,expected',[
    (ID,'# '+TITLE,TITLE),
    ('2510.12345v2','# '+TITLE,TITLE),
    (ID,'# 1 Introduction to Our Method',ID),
    (ID,'# Abstract About This Research Paper',ID),
    ('User Provided Title','# '+TITLE,'User Provided Title'),
])
def test_parsed_display_title_does_not_claim_or_guess_verified_metadata(title,heading,expected):
    assert parsed_display_title(title,heading)==expected


def test_structured_doi_conflict_cannot_be_bypassed_by_a_stripped_markdown_fallback(monkeypatch):
    monkeypatch.setattr('paper_library.metadata.queries.generate_search_plan',lambda *a,**k:{'status':'disabled'})
    gateway=Gateway()
    result=verify_bibliography(ID,'','',ID+'.pdf','# '+TITLE,gateway,
        known={'doi':'10.1000/a-different-paper'},acl_lookup=client())
    assert result.status=='conflict'
    assert result.bibliography=={}
    assert not gateway.extracted
    assert result.enrichment['candidates'][0]['reason']=='identifier'


def test_search_candidate_uses_structured_metadata_when_the_extract_is_incomplete(monkeypatch):
    monkeypatch.setattr('paper_library.metadata.queries.generate_search_plan',lambda *a,**k:{'status':'disabled'})
    class SearchGateway(Gateway):
        def search(self, query):
            return GatewayResponse(200,{'results':[{'title':TITLE,'url':URL,'content':'EMNLP 2025'}]})
        def extract(self, urls):
            raise AssertionError('Complete official metadata should not require a Markdown extract')
    result=verify_bibliography('custom filename','','','paper.pdf','# '+TITLE,SearchGateway(),acl_lookup=client())
    assert result.status=='verified'
    assert result.bibliography['doi']=='10.18653/v1/'+ID
    assert result.bibliography['authors']==['Jane Doe','José Roe']


def test_metadata_refresh_updates_citations_without_changing_paper_content(tmp_path,monkeypatch):
    import json
    from test_ingest_service import write_library
    from paper_library.catalog import PaperPaths
    from paper_library.ingest.service import refresh_metadata
    from paper_library.metadata.citations import build_citations
    pdf=tmp_path/'existing.pdf';pdf.write_bytes(b'%PDF-fixture')
    repository,library=write_library(tmp_path/'libraries',pdf)
    paths=PaperPaths.for_id(library,'ws-p0001')
    paths.markdown.write_text('# '+TITLE+'\n\nOriginal body text.\n')
    paths.metadata.write_text(json.dumps({'title':ID,'original_filename':ID+'.pdf','bibliography':{}}))
    original_pdf=paths.pdf.read_bytes();original_md=paths.markdown.read_bytes()
    monkeypatch.setattr('paper_library.metadata.queries.generate_search_plan',lambda *a,**k:{'status':'disabled'})
    monkeypatch.setattr('paper_library.metadata.identifiers.ACLClient',lambda:client())
    gateway=Gateway()
    monkeypatch.setattr('paper_library.ingest.service.GatewayClient',lambda:gateway)
    refresh_metadata(repository,'whitespace','ws-p0001')
    paper=repository.find_paper('whitespace','ws-p0001')
    metadata=__import__('json').loads(paths.metadata.read_text())
    assert (paper.title,paper.year,paper.venue)==(TITLE,'2025','Proceedings of EMNLP 2025')
    assert metadata['enrichment']['status']=='verified'
    citations=build_citations(paper,metadata)
    assert '@inproceedings' in citations['bibtex']
    assert 'Jane Doe and José Roe' in citations['bibtex']
    assert '10.18653/v1/'+ID in citations['bibtex']
    assert paths.pdf.read_bytes()==original_pdf and paths.markdown.read_bytes()==original_md
    assert next((paths.root/'metadata-repairs').glob('*/metadata.json')).is_file()
    assert not gateway.extracted and not gateway.searched


def test_new_ingest_uses_parsed_title_for_identifier_filename_without_claiming_verification(tmp_path):
    import json
    from test_ingest_service import write_library
    from paper_library.ingest.service import ingest_pdf
    existing=tmp_path/'existing.pdf';existing.write_bytes(b'%PDF-existing')
    repository,_=write_library(tmp_path/'libraries',existing)
    source=tmp_path/(ID+'.pdf');source.write_bytes(b'%PDF-new')
    class Runner:
        def run(self, source, output, language, **kwargs):
            (output/'images').mkdir(parents=True)
            (output/'article.md').write_text('# '+TITLE+'\n\nOriginal body text.\n')
    result=ingest_pdf(repository,'whitespace',source,mineru_runner=Runner(),enrich=False)
    metadata=json.loads(result.paths.metadata.read_text())
    assert repository.find_paper('whitespace',result.paper_id).title==TITLE
    assert metadata['title']==TITLE
    assert metadata['enrichment']['status']=='skipped'
    assert metadata['bibliography']=={}
