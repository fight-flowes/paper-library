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


@dataclass(frozen=True)
class TranslationSegment:
    id: str
    text: str
    protected: tuple[tuple[str, str], ...]
    numeric_markers: tuple[str, ...] = ()
    heading_prefix: str = ""
    formula_markers: tuple[str, ...] = ()

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
        prose = re.sub(r'⟦PH_(?:[a-f0-9]+_)?\d+⟧', '', translated)
        if re.search(r'[\d⁰¹²³⁴⁵⁶⁷⁸⁹]', prose):
            notes.append('译文包含新增数字，请按需对照原文')
        if self._format_changed(translated):
            notes.append('格式异常，已降级展示')
        if '〔译文未完整生成〕' in translated:
            notes.append('译文未完整生成')
        return notes

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
                    return protect(html.escape(fragment), numeric=True)
                parts.append(_QUANTITY.sub(protect_quantity, child.content))
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
