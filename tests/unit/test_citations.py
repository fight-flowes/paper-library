from types import SimpleNamespace


def cite(bibliography=None):
    from paper_library.metadata.citations import build_citations
    paper = SimpleNamespace(id='ex-p0001', display_title='Fallback title', year='2023', venue='')
    return build_citations(paper, {'bibliography': bibliography or {}})


def test_verified_fields_and_conference_bibtex():
    result = cite({'canonical_title': 'LLMs & Text', 'authors': ['Yiwei Wang', 'Yujun Cai'],
                   'venue_type': 'conference', 'venue': 'EMNLP', 'publication_date': '2024-12',
                   'doi': '10.1234/example'})
    assert result['plain'] == 'Yiwei Wang, Yujun Cai. LLMs & Text. EMNLP, 2024. https://doi.org/10.1234/example'
    assert result['bibtex'].startswith('@inproceedings{ex-p0001,')
    assert 'author = {Yiwei Wang and Yujun Cai}' in result['bibtex']
    assert 'title = {{LLMs \\& Text}}' in result['bibtex']
    assert 'booktitle = {EMNLP}' in result['bibtex']
    assert 'year = {2024}' in result['bibtex']


def test_missing_fields_are_omitted():
    result = cite()
    assert result['plain'] == 'Fallback title. 2023.'
    assert result['bibtex'].startswith('@misc{ex-p0001,')
    assert 'author =' not in result['bibtex']
    assert 'journal =' not in result['bibtex']
    assert 'doi =' not in result['bibtex']


def test_journal_and_escaping():
    result = cite({'canonical_title': 'A {test} 50% #1_', 'venue_type': 'journal', 'venue': 'Nature',
                   'authors': ['Research and Development'], 'official_url': 'https://example.org/a'})
    assert result['bibtex'].startswith('@article{')
    assert 'journal = {Nature}' in result['bibtex']
    assert 'author = {{Research and Development}}' in result['bibtex']
    assert r'\{test\} 50\% \#1\_' in result['bibtex']
    assert 'url = {https://example.org/a}' in result['bibtex']
