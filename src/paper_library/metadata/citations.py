"""Deterministic citations from existing bibliographic metadata."""
import re


def _text(value):
    return ' '.join(value.split()) if isinstance(value, str) else ''


def _tex(value):
    escapes = {'\\': r'\textbackslash{}', '{': r'\{', '}': r'\}', '&': r'\&',
               '%': r'\%', '$': r'\$', '#': r'\#', '_': r'\_',
               '~': r'\textasciitilde{}', '^': r'\textasciicircum{}'}
    return ''.join(escapes.get(char, char) for char in value)


def build_citations(paper, metadata):
    bibliography = metadata.get('bibliography') or {}
    if not isinstance(bibliography,dict):bibliography={}
    title = _text(bibliography.get('canonical_title')) or paper.display_title
    raw_authors = bibliography.get('authors') or []
    authors = [_text(author) for author in raw_authors if _text(author)] if isinstance(raw_authors, list) else []
    date = _text(bibliography.get('publication_date')) or _text(paper.year)
    year_match = re.match(r'^\d{4}\b', date)
    year = year_match.group() if year_match else ''
    venue = _text(bibliography.get('venue')) or _text(paper.venue)
    doi = _text(bibliography.get('doi'))
    doi = re.sub(r'^https?://(?:dx\.)?doi\.org/', '', doi, flags=re.I)
    url = _text(bibliography.get('official_url'))
    parts = [', '.join(authors), title, ', '.join(filter(None, [venue, year]))]
    plain = ' '.join(part.rstrip('.') + '.' for part in parts if part)
    if doi or url:
        plain += ' ' + ('https://doi.org/' + doi if doi else url)
    kind = _text(bibliography.get('venue_type'))
    entry = {'conference': 'inproceedings', 'journal': 'article'}.get(kind, 'misc')
    fields = [('title', '{' + _tex(title) + '}')]
    if authors:
        names = [('{' + _tex(name) + '}') if ' and ' in name else _tex(name) for name in authors]
        fields.append(('author', ' and '.join(names)))
    if venue:
        fields.append(({'conference': 'booktitle', 'journal': 'journal'}.get(kind, 'howpublished'), _tex(venue)))
    fields.extend((key, _tex(value)) for key, value in [('year', year), ('doi', doi), ('url', url)] if value)
    key = re.sub(r'[^A-Za-z0-9:_-]', '-', paper.id)
    bibtex = '@' + entry + '{' + key + ',\n' + ',\n'.join('  ' + name + ' = {' + value + '}' for name, value in fields) + '\n}'
    return {'plain': plain, 'bibtex': bibtex}
