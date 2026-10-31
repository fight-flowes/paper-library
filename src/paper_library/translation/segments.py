"""Align translations to Markdown blocks while protecting non-text inline tokens."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import html
import re

from markdown_it import MarkdownIt
from markdown_it.token import Token


# Keep quantities (including k/M, percentages, model versions and common units) exact.
_QUANTITY = re.compile(
    r'(?<!\w)[+-]?\w*\d\w*(?:[.,+-]\w+)*(?:[%‰]+)?'
    r'(?:[ \t]+(?:ms|ns|µs|μs|s|mm|cm|km|kg|mg|Hz|kHz|MHz|GHz|KB|MB|GB|TB|K|°C)(?!\w))?'
    r'(?!\w)|[⁰¹²³⁴⁵⁶⁷⁸⁹]+'
)
_MARKER = re.compile(r'⟦PH_[a-f0-9]+_\d+⟧')
_CHAT_CONTROL_TOKEN = re.compile(r'(<\|[^<>\n|]{1,64}\|?>)')


@dataclass(frozen=True)
class TranslationSegment:
    id: str
    text: str
    protected: tuple[tuple[str, str], ...]
    numeric_markers: tuple[str, ...] = ()
    heading_prefix: str = ""
    formula_markers: tuple[str, ...] = ()

    def literal_urls(self) -> tuple[str, ...]:
        text = self.text
        for marker, _ in self.protected:
            if marker not in self.numeric_markers:
                text = text.replace(marker, ' ')
        urls = []
        for match in re.finditer(r'https?://[^\s<>"`]+', text):
            url = match[0].rstrip('.,;:!?，。；：！？')
            pairs = {')': '(', ']': '[', '}': '{'}
            while url and url[-1] in pairs and url.count(url[-1]) > url.count(pairs[url[-1]]):
                url = url[:-1].rstrip('.,;:!?，。；：！？')
            urls.append(url)
        return tuple(urls)

    def warnings(self, translated: str) -> list[str]:
        expected = Counter(marker for marker, _ in self.protected)
        actual = Counter(_MARKER.findall(translated))
        notes = []
        missing = sum((expected - actual).values())
        extra = sum((actual - expected).values())
        if missing:
            notes.append(f'符号缺失：遗漏 {missing} 个原文标记，请对照原文')
        if extra:
            notes.append(f'重复或新增 {extra} 个标记，请对照原文')
        if re.search(r'⟦PH_\d+⟧', translated):
            notes.append('符号缺失：存在无法识别的标记')
        if '⟦PH_' in _MARKER.sub('', re.sub(r'⟦PH_\d+⟧', '', translated)):
            notes.append('符号缺失：存在损坏的标记')
        prose = re.sub(r'⟦PH_(?:[a-f0-9]+_)?\d+⟧', '', translated)
        if re.search(r'[\d⁰¹²³⁴⁵⁶⁷⁸⁹]', prose):
            notes.append('译文包含新增数字，请按需对照原文')
        if self._format_changed(translated):
            notes.append('格式异常，已降级展示')
        if '〔译文未完整生成〕' in translated:
            notes.append('译文未完整生成')
        if '〔解析文本变量缺失〕' in translated:
            notes.append('解析文本缺少变量，请对照 PDF')
        if any(translated.count(url) < count for url, count in Counter(self.literal_urls()).items()):
            notes.append('原文链接被改写或遗漏，请对照原文')
        return notes

    def integrity_warnings(self, translated: str) -> list[str]:
        """Distinguish missing source content from advisory generated digits."""
        return [note for note in self.warnings(translated)
                if note not in {'译文包含新增数字，请按需对照原文', '解析文本缺少变量，请对照 PDF'}]

    def integrity_score(self, translated: str) -> int:
        expected = Counter(marker for marker, _ in self.protected)
        actual = Counter(_MARKER.findall(translated))
        return (sum((expected - actual).values()) + sum((actual - expected).values())
                + len(self.integrity_warnings(translated))
                + (100 if '〔译文未完整生成〕' in translated else 0))

    def _format_changed(self, translated: str) -> bool:
        # Only paired formatting tags need strict ordering for safe HTML.
        tags = {marker for marker, fragment in self.protected
                if re.fullmatch(r'</?(?:a|strong|em|s)(?:\s[^>]*)?>', fragment)}
        return ([m for m in _MARKER.findall(translated) if m in tags]
                != [m for m, _ in self.protected if m in tags])

    def render_translation(self, translated: str) -> str:
        """Keep usable prose; degrade broken formatting without trusting model HTML."""
        if not translated.strip():
            raise ValueError('译文为空')
        broken_format = self._format_changed(translated)
        fragments = dict(self.protected)
        # Normalize whitespace only around known hard-break placeholders.
        for marker, fragment in self.protected:
            if fragment.strip() in {'<br />', '<br>'}:
                translated = re.sub(r'[ \t\r\n]*' + re.escape(marker) + r'[ \t\r\n]*',
                                    lambda _: marker, translated)
        output = html.escape(translated)
        def restore(match):
            fragment = fragments.get(match.group())
            if fragment is None:
                return '〔符号缺失〕'
            if broken_format and re.fullmatch(r'</?(?:a|strong|em|s)(?:\s[^>]*)?>', fragment):
                return ''
            return fragment.strip() if fragment.strip() in {'<br />', '<br>'} else fragment
        output = re.sub(r'⟦PH_(?:[a-f0-9]+_)?\d+⟧', restore, output)
        output = re.sub(r'⟦PH_[A-Za-z0-9_]*(?:⟧)?', '〔符号缺失〕', output)
        notes = [note for note in self.warnings(translated)
                 if note not in {'译文未完整生成', '译文包含新增数字，请按需对照原文'}]
        if notes:
            output += '<span class="translation-warning">〔' + html.escape('；'.join(notes)) + '〕</span>'
        return output


def add_translation_slots(tokens: list[Token], parser: MarkdownIt) -> tuple[TranslationSegment, ...]:
    segments = []
    insertions = []
    for index, token in enumerate(tokens):
        if token.type != "inline" or not token.children or index == 0 or index + 1 >= len(tokens):
            continue
        parent = tokens[index - 1]
        if parent.type not in {"paragraph_open", "heading_open", "th_open", "td_open"}:
            continue
        # Image captions may contain text, but images/formulas alone are not translation units.
        prose = "".join(child.content for child in token.children if child.type == "text")
        if not re.search(r'[A-Za-z]{2}', prose):
            continue
        digest = hashlib.sha256(f'{index}:{token.content}'.encode()).hexdigest()[:24]
        parts = []
        protected = []
        numeric_markers = []
        formula_markers = []
        def protect(fragment, *, numeric=False):
            marker = f'⟦PH_{digest}_{len(protected)}⟧'
            protected.append((marker, fragment))
            if numeric:
                numeric_markers.append(marker)
            return marker

        for child in token.children:
            if child.type == "image":
                # The original image is already visible above the translated caption.
                continue
            if child.type == "hardbreak" and not any(part.strip() for part in parts):
                continue
            if child.type == "text":
                def protect_quantity(match):
                    fragment = match.group()
                    # PDF extraction often glues citation digits to ordinary prose.
                    # Keep that prose visible to the translator. Mixed-case model
                    # names, single-letter variables and numeric units stay intact;
                    # inline code is handled separately below.
                    citation = re.fullmatch(r'([a-z]{2,})(\d+(?:[,+-]\d+)*)', fragment)
                    if citation:
                        return citation[1] + protect(citation[2], numeric=True)
                    # PDF extraction often joins an affiliation or footnote number
                    # to a capitalized name. Preserve only the number so the name
                    # remains available to the translator. Model names such as
                    # GPT4All and mixed alphanumeric versions stay protected.
                    labeled_name = re.fullmatch(r'(\d{1,3})([A-Z][a-z]{2,}[A-Za-z]*|We|In|As|It|On|By|An|At|To|Of|Is|If|Or|No|Do|He|So)', fragment)
                    if labeled_name:
                        return protect(labeled_name[1], numeric=True) + labeled_name[2]
                    return protect(html.escape(fragment), numeric=True)
                for piece in _CHAT_CONTROL_TOKEN.split(child.content):
                    if not piece:
                        continue
                    if _CHAT_CONTROL_TOKEN.fullmatch(piece):
                        parts.append(protect(html.escape(piece)))
                    else:
                        parts.append(_QUANTITY.sub(protect_quantity, piece))
            elif child.type == "softbreak":
                parts.append('\n')
            else:
                fragment = parser.renderer.renderInline([child], parser.options, {})
                marker = protect(fragment)
                if child.type == 'math_inline':
                    formula_markers.append(marker)
                parts.append(marker)
        text = ''.join(parts)
        heading_prefix = ''
        if parent.type == 'heading_open' and protected:
            marker, fragment = protected[0]
            if re.fullmatch(r'\d+(?:\.\d+)*', fragment):
                match = re.match(re.escape(marker) + r'[.)]?\s+', text)
                if match:
                    heading_prefix = match.group()
        segment = TranslationSegment(digest, text, tuple(protected), tuple(numeric_markers), heading_prefix,
                                     tuple(formula_markers))
        segments.append(segment)
        slot = Token('html_block', '', 0)
        slot.content = (f'<div class="paragraph-translation" data-translation-id="{digest}" '
                        'lang="zh-CN" hidden></div>\n')
        # A cell translation belongs inside its cell; others follow the closing block tag.
        position = index + 1 if parent.type in {'th_open', 'td_open'} else index + 2
        insertions.append((position, slot))
    for position, slot in reversed(insertions):
        tokens.insert(position, slot)
    return tuple(segments)
