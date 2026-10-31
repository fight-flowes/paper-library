"""Select overview evidence from physical source boundaries, independent of the TOC."""
from paper_library.structure.frontmatter import (
    appendix_boundary, primary_article, section_label, source_headings,
)


def overview_ids(source, document, annotations=None):
    headings = source_headings(source)
    article = primary_article(source)
    if headings and section_label(headings[0].title) == 'research briefing' and article is None:
        raise ValueError('组合文档的正式正文位置无法唯一确定，请核对标题与 DOI')
    start = article.start_line if article else 1
    end = article.end_line if article else len(source.splitlines()) + 1
    boundaries = [(h.start_line, section_label(h.title), h.level) for h in headings]
    # Semantic headings can originate in plain paragraphs, including a
    # References label that the renderer correctly leaves out of the TOC.
    for section in document['sections']:
        line = document.get('section_lines', {}).get(section['id'])
        if line:
            boundaries.append((line, section_label(section['title']), section['level']))
    boundaries = sorted(set(boundaries))
    reference_labels = {'references', 'bibliography', '参考文献'}
    references = [(line, level) for line, label, level in boundaries
                  if start <= line < end and label in reference_labels]
    # Nature places Methods after the main article's references. Its own DOI
    # anchors that layout; ordinary papers stop at their first bibliography.
    nature_methods = (article is not None and article.doi.casefold().startswith('10.1038/')
                      and any(label == 'methods' and line > start for line, label, _ in boundaries))
    if references and not nature_methods:
        end = min(end, references[0][0])
    for line, label, _ in boundaries:
        if start < line < end and appendix_boundary(label):
            end = line
    excluded = []
    for line, level in references:
        following = next((n for n, label, depth in boundaries
                          if n > line and (depth <= level or label == 'methods')), end)
        excluded.append((line, following))
    reference_ids = {row['id'] for row in annotations or [] if row['role'] == 'references'}
    return {block['id'] for block in document['blocks']
            if start <= document['block_lines'][block['id']] < end
            and not any(a <= document['block_lines'][block['id']] < b for a, b in excluded)
            and f"source-{document['block_lines'][block['id']]}" not in reference_ids}
