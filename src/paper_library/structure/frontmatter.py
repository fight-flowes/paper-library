"""Locate an article's own front matter in parsed text, without opening a PDF."""
from dataclasses import dataclass
import re

from markdown_it import MarkdownIt
from mdit_py_plugins.dollarmath import dollarmath_plugin


@dataclass(frozen=True)
class SourceHeading:
    start_line: int
    end_line: int
    title: str
    level: int


def source_headings(source: str) -> list[SourceHeading]:
    parser = MarkdownIt('commonmark').use(dollarmath_plugin, allow_labels=False)
    tokens = parser.parse(source)
    return [SourceHeading(token.map[0] + 1, token.map[1] + 1,
                          tokens[index + 1].content.strip(), int(token.tag[1:]))
            for index, token in enumerate(tokens)
            if token.type == 'heading_open' and token.level == 0 and token.map]


def section_label(title: str) -> str:
    label = re.sub(r'[*_`]', '', title).strip().strip(':：').casefold()
    return re.sub(r'^(?:(?:\d+|[a-z]|[ivxlcdm]+)(?:\.\d+)*[.)]?\s+)', '', label)


def appendix_boundary(title: str) -> bool:
    return bool(re.match(r'^(?:appendi(?:x|ces)\b|supplementary (?:information|materials)\b|'
                         r'supporting information\b|reporting summary$)', section_label(title)))


@dataclass(frozen=True)
class ArticleFrontmatter:
    title: str
    doi: str
    start_line: int
    end_line: int
    text: str
    combined: bool


def primary_article(source: str) -> ArticleFrontmatter | None:
    """Require a title immediately followed by its own DOI, never a cited DOI.

    A Nature research briefing may precede the actual article. Outside that
    explicit layout, later headings are not promoted to bibliographic titles.
    """
    lines = source.splitlines(keepends=True)
    headings = source_headings(source)
    if not headings:
        return None
    labels = {'research briefing', 'article', 'letter', 'research article'}
    combined = section_label(headings[0].title) == 'research briefing'
    candidates = []
    for heading in headings:
        start, after, title = heading.start_line - 1, heading.end_line - 1, heading.title
        if appendix_boundary(title):
            break
        if section_label(title) in labels | {'references', 'bibliography', 'abstract', 'introduction'}:
            continue
        following = ''.join(lines[after:after + 6])
        match = re.match(r'\s*(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)'
                         r'(10\.\d{4,9}/[A-Za-z0-9._;()/:+-]+)[ \t]*(?:\n|$)', following, re.I)
        if match:
            end = next((h.start_line - 1 for h in headings if h.start_line > start + 1
                        and appendix_boundary(h.title)), len(lines))
            candidates.append(ArticleFrontmatter(title, match[1].rstrip('.,;'), start + 1, end + 1,
                                                ''.join(lines[start:min(start + 120, end)])[:8000], combined))
        if not combined:
            break
    identities = {(item.title.casefold(), item.doi.casefold()) for item in candidates}
    return candidates[0] if len(identities) == 1 else None
