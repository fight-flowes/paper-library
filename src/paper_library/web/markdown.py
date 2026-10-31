"""Safe, offline Markdown rendering for MinerU paper packages."""

from __future__ import annotations

from functools import lru_cache
import html
import re
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Iterable
from urllib.parse import quote, unquote, urlsplit

import bleach
from latex2mathml.converter import convert
from markdown_it import MarkdownIt
from markdown_it.token import Token
from mdit_py_plugins.dollarmath import dollarmath_plugin

from paper_library.catalog import IMAGE_SUFFIXES
from paper_library.translation.segments import TranslationSegment, add_translation_slots


_TABLE_TAGS = frozenset(
    {"table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption", "br"}
)
_TABLE_ATTRIBUTES = {
    "th": ["colspan", "rowspan", "scope"],
    "td": ["colspan", "rowspan"],
}


@dataclass(frozen=True, slots=True)
class Heading:
    level: int
    id: str
    text: str
    source_line: int = 0


@dataclass(frozen=True, slots=True)
class RenderedMarkdown:
    html: str
    headings: tuple[Heading, ...]
    warnings: tuple[str, ...]
    segments: tuple[TranslationSegment, ...] = ()
    physical_headings: tuple[Heading, ...] = ()


def render_markdown(source: str, asset_url_prefix: str, *, translation_slots: bool = False, annotations=None, image_size=None) -> RenderedMarkdown:
    """Render one Markdown string without filesystem or network access."""

    failed_formula_count = 0

    def render_formula(content: str, options: dict[str, Any]) -> str:
        nonlocal failed_formula_count
        display = "block" if options.get("display_mode") else "inline"
        try:
            return convert(content, display=display)
        except Exception:
            failed_formula_count += 1
            return f'<code class="math-fallback">{html.escape(content)}</code>'

    parser = MarkdownIt(
        "commonmark",
        {"html": True, "linkify": False, "typographer": False},
    )
    parser.enable("table")
    parser.use(
        dollarmath_plugin,
        allow_labels=False,
        renderer=render_formula,
    )

    tokens = parser.parse(source)
    _sanitize_raw_html(tokens)
    _rewrite_images(tokens, asset_url_prefix)
    headings = _assign_heading_ids(tokens)
    physical_headings = headings
    # Source-line anchors do not alter translation IDs or token positions.
    for token in tokens:
        if token.level == 0 and token.map:
            anchor = f'source-{token.map[0] + 1}'
            if token.type in {'paragraph_open', 'table_open', 'blockquote_open', 'bullet_list_open', 'ordered_list_open'}:
                token.attrSet('id', anchor)
            elif token.type == 'html_block':
                token.content = f'<div id="{anchor}">{token.content}</div>'
            elif token.type in {'fence', 'code_block', 'math_block'}:
                token.attrSet('id', anchor)
    # A journal's abstract/introduction may have no heading. Do not treat all
    # text before its first numbered section as author/publication metadata.
    heading_positions = [i for i, token in enumerate(tokens) if token.type == 'heading_open']
    if len(heading_positions) > 1 and heading_positions[0] == 0:
        for index in range(1, heading_positions[1]):
            token = tokens[index]
            if token.type == 'paragraph_open' and token.level == 0:
                content = tokens[index + 1].content.strip()
                # Prefer normal body styling when a leading paragraph is long
                # or contains a prose sentence, even if metadata is ambiguous.
                if len(content) >= 180 or re.search(r'[a-z]{3}[.!?](?:\s|$)', content):
                    break
                token.attrJoin('class', 'paper-frontmatter')
    segments = add_translation_slots(tokens, parser) if translation_slots else ()
    if annotations is not None:
        from paper_library.structure.document import build_blocks, validate_annotations
        try:
            roles = {r['id']: r for r in validate_annotations(annotations, build_blocks(source))}
        except (ValueError, TypeError):
            roles = {}
        original_headings = {heading.id: heading for heading in headings}
        adjusted = []
        for index, token in enumerate(tokens):
            if token.type == 'heading_open' and token.level != 0:
                adjusted.append(original_headings[token.attrGet('id')])
                continue
            if token.level != 0 or not token.map or token.type not in {'paragraph_open', 'heading_open'}:
                continue
            row = roles.get(f'source-{token.map[0] + 1}', {})
            role = row.get('role', 'unknown')
            if role not in {'unknown', 'preserve'}:
                token.attrs.pop('class', None)
                token.attrJoin('class', 'structure-' + role)
                if role in {'authors', 'affiliation', 'publication'}:
                    token.attrJoin('class', 'paper-frontmatter')
                if role == 'heading':
                    if token.type == 'heading_open':
                        token.tag = 'h' + str(row['level'])
                        tokens[index + 2].tag = token.tag
                    else:
                        token.attrSet('role', 'heading')
                        token.attrSet('aria-level', str(row['level']))
            if row.get('toc') is False and token.type == 'heading_open' and role != 'title':
                token.tag = 'p'
                tokens[index + 2].tag = 'p'
            if row.get('toc', token.type == 'heading_open' or role == 'heading'):
                level = row['level'] if role == 'heading' else int(token.tag[1:])
                anchor = token.attrGet('id')
                text = (original_headings[anchor].text if anchor in original_headings else
                        ''.join(child.content for child in tokens[index + 1].children or []
                                if child.type in {'text', 'code_inline', 'math_inline'}))
                adjusted.append(Heading(level, anchor, text, token.map[0] + 1))
        headings = tuple(adjusted)
    for index in range(len(tokens) - 1, -1, -1):
        token = tokens[index]
        if token.level == 0 and token.map and token.type in {'fence', 'code_block', 'math_block'}:
            anchor_token = Token('html_block', '', 0)
            anchor_token.content = f'<span id="source-{token.map[0] + 1}"></span>'
            token.attrs.pop('id', None)
            tokens.insert(index, anchor_token)
    # Display attributes must not change protected translation fragments/cache keys.
    for token in _walk_tokens(tokens):
        if token.type == 'image':
            token.attrSet('loading', 'lazy')
            token.attrSet('decoding', 'async')
            if image_size:
                dimensions = image_size(token.attrGet('src') or '')
                if dimensions:
                    token.attrSet('width', str(dimensions[0]))
                    token.attrSet('height', str(dimensions[1]))
    rendered_html = parser.renderer.render(tokens, parser.options, {})

    warnings: tuple[str, ...] = ()
    if failed_formula_count:
        warnings = (
            f"有 {failed_formula_count} 个公式无法转换，已显示 LaTeX 原文。",
        )
    return RenderedMarkdown(rendered_html, headings, warnings, segments, physical_headings)


def _walk_tokens(tokens: Iterable[Token]) -> Iterable[Token]:
    for token in tokens:
        yield token
        if token.children:
            yield from _walk_tokens(token.children)


def _sanitize_raw_html(tokens: Iterable[Token]) -> None:
    tokens = list(tokens)
    # MinerU may leave prose references to HTML tags without Markdown backticks.
    # Recognize explicit tag descriptions, not genuine layout markup.
    for parent in _walk_tokens(tokens):
        children = parent.children or []
        for index, child in enumerate(children[:-1]):
            following = children[index + 1]
            if (child.type == 'html_inline'
                    and re.fullmatch(r'</?(?:div|p|br)\s*/?>', child.content, re.I)
                    and following.type == 'text'
                    and re.match(r'\s+(?:elements?\b|tags?\b|line breaks?\b)', following.content, re.I)):
                # Keep an inline HTML token so translation IDs/placeholders and
                # existing cache keys remain unchanged; escape its display only.
                child.content = '<code>' + html.escape(child.content) + '</code>'
                child.meta['literal_tag_reference'] = True
    for token in _walk_tokens(tokens):
        if token.type not in {"html_block", "html_inline"}:
            continue
        if token.meta.get('literal_tag_reference'):
            continue
        token.content = bleach.clean(
            token.content,
            tags=_TABLE_TAGS,
            attributes=_TABLE_ATTRIBUTES,
            protocols=frozenset({"http", "https", "mailto"}),
            strip=True,
            strip_comments=True,
        )


def _rewrite_images(tokens: Iterable[Token], asset_url_prefix: str) -> None:
    prefix = asset_url_prefix.rstrip("/")
    for token in tokens:
        if token.children:
            safe_children: list[Token] = []
            for child in token.children:
                if child.type == "image":
                    rewritten = _safe_image_url(child.attrGet("src") or "", prefix)
                    if rewritten is None:
                        continue
                    child.attrSet("src", rewritten)
                if child.children:
                    _rewrite_images((child,), prefix)
                safe_children.append(child)
            token.children = safe_children


def _safe_image_url(source: str, prefix: str) -> str | None:
    parsed = urlsplit(source)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return None

    decoded_path = unquote(parsed.path)
    relative = PurePosixPath(decoded_path)
    if (
        not decoded_path
        or "\x00" in decoded_path
        or "\\" in decoded_path
        or relative.is_absolute()
        or ".." in relative.parts
        or relative.suffix.casefold() not in IMAGE_SUFFIXES
    ):
        return None
    return f"{prefix}/{quote(relative.as_posix(), safe='/')}"


def _assign_heading_ids(tokens: list[Token]) -> tuple[Heading, ...]:
    headings: list[Heading] = []
    slug_counts: dict[str, int] = {}
    for index, token in enumerate(tokens[:-1]):
        if token.type != "heading_open":
            continue
        inline = tokens[index + 1]
        if inline.type != "inline":
            continue
        text = _inline_text(inline.children or ()).strip()
        base_slug = _slugify(text)
        slug_counts[base_slug] = slug_counts.get(base_slug, 0) + 1
        count = slug_counts[base_slug]
        heading_id = base_slug if count == 1 else f"{base_slug}-{count}"
        token.attrSet("id", heading_id)
        headings.append(Heading(int(token.tag[1:]), heading_id, text, token.map[0] + 1 if token.map else 0))
    return tuple(headings)


def _inline_text(tokens: Iterable[Token]) -> str:
    parts: list[str] = []
    for token in tokens:
        if token.type in {"text", "code_inline", "math_inline"}:
            parts.append(token.content)
        elif token.type == "image":
            parts.append(token.content)
        elif token.children:
            parts.append(_inline_text(token.children))
    return "".join(parts)


def _slugify(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    slug = re.sub(r"[^\w-]+", "-", normalized, flags=re.UNICODE)
    slug = re.sub(r"-+", "-", slug).strip("-_")
    return slug or "section"


@lru_cache(maxsize=8)
def render_translatable_markdown(source: str, asset_url_prefix: str) -> RenderedMarkdown:
    """Reuse immutable rendering during sequential paragraph requests."""
    return render_markdown(source, asset_url_prefix, translation_slots=True)
