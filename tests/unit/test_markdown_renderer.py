from __future__ import annotations

from unittest.mock import Mock

from paper_library.web.markdown import render_markdown


def test_render_markdown_builds_unique_heading_ids_and_toc() -> None:
    result = render_markdown("# Intro\n\n## Intro\n", "/asset")

    assert [(heading.level, heading.id, heading.text) for heading in result.headings] == [
        (1, "intro", "Intro"),
        (2, "intro-2", "Intro"),
    ]
    assert '<h1 id="intro">' in result.html
    assert '<h2 id="intro-2">' in result.html


def test_heading_without_letters_gets_stable_fallback_id() -> None:
    result = render_markdown("# 论文阅读\n\n## !!!\n", "/asset")

    assert [heading.id for heading in result.headings] == ["论文阅读", "section"]


def test_render_markdown_rewrites_only_safe_relative_images() -> None:
    source = "![ok](<images/figure one.jpg>)\n\n![bad](../../secret.png)"

    result = render_markdown(source, "/paper/roman/asset")

    assert '/paper/roman/asset/images/figure%20one.jpg' in result.html
    assert "secret.png" not in result.html
    assert result.html.count("<img") == 1


def test_render_markdown_rejects_external_images() -> None:
    result = render_markdown("![remote](https://example.com/pixel.png)", "/asset")

    assert "example.com" not in result.html
    assert "<img" not in result.html


def test_render_markdown_keeps_mineru_table_but_removes_script() -> None:
    source = (
        '<table><tr><td colspan="2" onclick="steal()">value</td></tr></table>'
        '<script>alert(1)</script>'
    )

    result = render_markdown(source, "/asset")

    assert "<table>" in result.html
    assert 'colspan="2"' in result.html
    assert "onclick" not in result.html
    assert "<script" not in result.html


def test_render_markdown_converts_inline_and_block_math_to_mathml() -> None:
    result = render_markdown("Inline $x^2$\n\n$$\n\\frac{a}{b}\n$$", "/asset")

    assert result.html.count("<math") == 2
    assert 'display="block"' in result.html
    assert result.warnings == ()


def test_invalid_formula_falls_back_to_escaped_latex(monkeypatch) -> None:
    monkeypatch.setattr(
        "paper_library.web.markdown.convert",
        Mock(side_effect=RuntimeError("bad")),
    )

    result = render_markdown("$<bad>$", "/asset")

    assert "&lt;bad&gt;" in result.html
    assert result.warnings == ("有 1 个公式无法转换，已显示 LaTeX 原文。",)


def test_fenced_code_and_markdown_table_render_without_raw_html() -> None:
    source = "```python\nprint('ok')\n```\n\n| A | B |\n| - | - |\n| 1 | 2 |"

    result = render_markdown(source, "/asset")

    assert '<code class="language-python">' in result.html
    assert '<table id="source-5">' in result.html
    assert "<td>1</td>" in result.html


def test_unsafe_link_protocol_is_not_rendered_as_a_link() -> None:
    result = render_markdown("[click](javascript:alert(1))", "/asset")

    assert "href=" not in result.html
    assert "<a " not in result.html
