"""Actual document sections and bounded, lossless source chunks."""
import hashlib
import re

from markdown_it import MarkdownIt
from mdit_py_plugins.dollarmath import dollarmath_plugin

from paper_library.web.markdown import _assign_heading_ids

BLOCK_TYPES = {'paragraph_open', 'table_open', 'html_block', 'fence', 'code_block',
               'math_block', 'blockquote_open', 'bullet_list_open', 'ordered_list_open'}


def build_document(source, annotations=None):
    parser = MarkdownIt('commonmark', {'html': True}).enable('table').use(dollarmath_plugin, allow_labels=False)
    tokens = parser.parse(source)
    headings = iter(_assign_heading_ids(tokens))
    effective = None
    if annotations is not None:
        from paper_library.web.markdown import render_markdown
        effective = {h.id: h for h in render_markdown(source, '/assets', annotations=annotations).headings}
    lines = source.splitlines(keepends=True)
    sections, blocks = [], []
    current = None
    for token in tokens:
        heading = next(headings) if token.type == 'heading_open' else None
        if effective is not None:
            key = heading.id if heading else f'source-{token.map[0]+1}' if token.map else ''
            heading = effective.get(key) if token.type == 'heading_open' or (token.type == 'paragraph_open' and token.level == 0) else None
        if heading is not None:
            numbered = re.match(r'^((?:\d+|[A-Z])(?:\.\d+)*)\.?\s+', heading.text)
            level = len(numbered.group(1).split('.')) if numbered and effective is None else heading.level
            current = {'id': heading.id, 'title': heading.text, 'level': level,
                       'blocks': [], 'parent': None}
            for previous in reversed(sections):
                if previous['level'] < level:
                    current['parent'] = previous['id']
                    break
            sections.append(current)
        elif token.level == 0 and token.map and token.type in BLOCK_TYPES:
            if current is None:
                current = {'id': f'source-{token.map[0] + 1}', 'title': '正文', 'level': 1, 'blocks': [], 'parent': None}
                sections.append(current)
            start, end = token.map
            block = {'id': f'source-{start + 1}', 'text': ''.join(lines[start:end]).strip()}
            if block['text']:
                blocks.append(block)
                current['blocks'].append(block)
    return {'document_version': hashlib.sha256(source.encode()).hexdigest(),
            'sections': sections, 'blocks': blocks}


def chunk_section(section, limit):
    chunks, current, size = [], [], 0
    for block in section['blocks']:
        for offset in range(0, len(block['text']), limit):
            part = {'id': block['id'], 'text': block['text'][offset:offset + limit]}
            if current and size + len(part['text']) > limit:
                chunks.append(current)
                current, size = [], 0
            current.append(part)
            size += len(part['text'])
    if current:
        chunks.append(current)
    return chunks
