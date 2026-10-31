"""Source-bound semantic roles; never rewrite original text."""
import hashlib
import json
from pathlib import Path

from markdown_it import MarkdownIt
from mdit_py_plugins.dollarmath import dollarmath_plugin

VERSION = 1
ROLES = frozenset({'title', 'authors', 'affiliation', 'publication', 'abstract', 'body',
                   'heading', 'caption', 'footnote', 'references', 'poetry', 'preserve', 'unknown'})
TYPES = {'paragraph_open', 'heading_open', 'table_open', 'html_block', 'fence', 'code_block',
         'math_block', 'blockquote_open', 'bullet_list_open', 'ordered_list_open', 'hr'}


def source_hash(source):
    return hashlib.sha256(source.encode('utf-8')).hexdigest()


def build_blocks(source):
    parser = MarkdownIt('commonmark', {'html': True}).enable('table').use(dollarmath_plugin, allow_labels=True)
    tokens = parser.parse(source)
    lines = source.splitlines(keepends=True)
    blocks = []
    for i, token in enumerate(tokens):
        if token.level != 0 or not token.map or token.type not in TYPES:
            continue
        start, end = token.map
        inline = tokens[i + 1] if i + 1 < len(tokens) else None
        children = inline.children or [] if inline and inline.type == 'inline' else []
        locked = (token.type not in {'paragraph_open', 'heading_open'}
                  or any(c.type in {'image', 'hardbreak', 'html_inline'} for c in children))
        blocks.append({'id': f'source-{start + 1}', 'text': ''.join(lines[start:end]),
                       'type': token.type, 'level': int(token.tag[1:]) if token.type == 'heading_open' else None,
                       'locked': locked})
    return blocks


def validate_annotations(rows, blocks):
    if not isinstance(rows, list) or len(rows) != len(blocks):
        raise ValueError('结构标注数量与原文不一致')
    by_id = {b['id']: b for b in blocks}
    found = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str):
            raise ValueError('无效结构标注')
        key, role = row['id'], row.get('role')
        if key not in by_id or key in found or not isinstance(role, str) or role not in ROLES:
            raise ValueError('结构标注包含未知、重复标识或角色')
        block = by_id[key]
        if block['locked']:
            role = 'preserve'
        level = row.get('level')
        if role == 'heading' and (type(level) is not int or not 1 <= level <= 6):
            raise ValueError('标题层级必须为 1 到 6')
        # Long prose must not become a heading/title even on a model mistake.
        if role in {'heading', 'title'} and len(block['text'].strip()) > 240:
            role = 'unknown'
        found[key] = {'id':key, 'role':role, **({'level':level} if role == 'heading' else {})}
        if 'toc' in row and not block['locked']:
            if type(row['toc']) is not bool or role in {'unknown', 'preserve'} or row['toc'] != (role == 'heading'):
                raise ValueError('目录成员标注与文本角色不一致')
            found[key]['toc'] = row['toc']
    return [found[b['id']] for b in blocks]


def load_structure(root: Path, source: str):
    try:
        data = json.loads((root / 'document-structure.json').read_text(encoding='utf-8'))
        if not isinstance(data, dict) or data.get('version') != VERSION or data.get('source_sha256') != source_hash(source):
            return None
        data['annotations'] = validate_annotations(data['annotations'], build_blocks(source))
        return data
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        return None
