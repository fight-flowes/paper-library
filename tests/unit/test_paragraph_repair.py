import json

import httpx
import pytest

from paper_library.config import Settings, ProjectPaths
from paper_library.ingest.paragraphs import candidates, repair, repair_file

LEFT = 'Whitespace not only has consequences for poetry but for NLP more broadly. For LLMs, it turns out, whitespace also matters. Research has shown'
RIGHT = 'that some models fail to account for the visual dimensions of text, and that adversarial attacks can exploit these challenges.'
SOURCE = LEFT + '\n\n' + RIGHT + '\n'


def settings(tmp_path):
    return Settings.load(ProjectPaths.from_root(tmp_path), {
        'PAPER_TRANSLATION_PROFILE': 'test-model', 'LLM_TEST_MODEL': 'test-model',
        'LLM_TEST_BASE_URL': 'http://model/v1', 'LLM_TEST_MAX_CONCURRENCY': '2',
    })


def transport(decision):
    return httpx.MockTransport(lambda request: httpx.Response(200, json={
        'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({
            'decision': decision, 'reason': '句法接续', 'rewritten_text': 'DO NOT USE'
        })}}]
    }))


def test_model_merge_changes_only_separator_and_keeps_backup(tmp_path):
    path = tmp_path / 'article.md'
    path.write_text(SOURCE)
    report = repair_file(path, settings=settings(tmp_path), transport=transport('merge'))
    assert path.read_text() == LEFT + ' ' + RIGHT + '\n'
    assert report['merged'] == 1
    assert next((tmp_path / 'paragraph-repairs').glob('*/original.md')).read_text() == SOURCE
    assert repair_file(path, settings=settings(tmp_path), transport=transport('merge'))['merged'] == 0


@pytest.mark.parametrize('decision', ['keep', 'uncertain', 'invalid'])
def test_non_merge_decisions_preserve_source(tmp_path, decision):
    output, report = repair(SOURCE, settings=settings(tmp_path), transport=transport(decision))
    assert output == SOURCE
    assert report['merged'] == 0


def test_model_outage_preserves_source(tmp_path):
    output, report = repair(SOURCE, settings=settings(tmp_path), transport=httpx.MockTransport(
        lambda request: httpx.Response(503)))
    assert output == SOURCE
    assert report['decisions'][0]['decision'] == 'uncertain'


@pytest.mark.parametrize('source', [
    '```\n' + SOURCE + '```\n',
    '> ' + LEFT + '\n>\n> ' + RIGHT,
    LEFT + '\n\n# Heading\n\n' + RIGHT,
    'a poetic line\nanother poetic line\n\n' + RIGHT,
    '- ' + LEFT + '\n\n- ' + RIGHT,
])
def test_structured_content_not_selected(source):
    assert candidates(source) == []


def test_cancellation_keeps_source(tmp_path):
    output, report = repair(SOURCE, settings=settings(tmp_path), transport=transport('merge'), cancelled=lambda: True)
    assert output == SOURCE
    assert report['merged'] == 0


def test_parenthetical_citation_and_literal_html_tag_are_candidates(tmp_path):
    left = LEFT + ' including OpenWebMath'
    right = '(Paster et al., 2023), and DataComp-LM. ' + RIGHT + ' We isolate the <div> elements containing poems.'
    source = left + '\n\n' + right + '\n'
    output, report = repair(source, settings=settings(tmp_path), transport=transport('merge'))
    assert report['merged'] == 1
    assert output == left + ' ' + right + '\n'


def test_inline_html_structure_is_still_excluded():
    assert candidates(LEFT + '\n\n' + RIGHT + '<br>continued') == []


@pytest.mark.parametrize('block', [
    '![](images/figure.jpg)  \nFigure 1: Caption retained exactly.',
    'Table 1: Results\n\n<table><tr><td>Score</td></tr></table>',
    '| Model | Score |\n| --- | --- |\n| Qwen | 90 |\n\nTable 2: Results',
])
def test_floating_blocks_move_after_complete_prose(tmp_path, block):
    left = LEFT + ' (Devlin et al.,'
    right = '2019; Sanh et al., 2020) ' + RIGHT
    source = left + '\n\n' + block + '\n\n' + right + '\n'
    output, report = repair(source, settings=settings(tmp_path), transport=transport('merge'))
    assert output == left + ' ' + right + '\n\n' + block + '\n'
    assert report['merged'] == 1
    assert repair(output, settings=settings(tmp_path), transport=transport('merge'))[0] == output


def test_media_does_not_allow_skipping_unrelated_content(tmp_path):
    for extra in ('# Section', 'Unrelated short paragraph.', '> Quoted text', '```\ncode\n```'):
        source = LEFT + '\n\n![](image.png)\n\n' + extra + '\n\n' + RIGHT
        assert not any(c['left'] == LEFT and c['right'] == RIGHT for c in candidates(source))


def test_chained_floating_blocks_keep_original_order(tmp_path):
    middle = RIGHT.rstrip('.') + ' (Author et al.,'
    end = '2020; Other, 2021) ' + RIGHT
    first = '![](one.png)\nFigure 1: First'
    second = '![](two.png)\nFigure 2: Second'
    source = LEFT + '\n\n' + first + '\n\n' + middle + '\n\n' + second + '\n\n' + end + '\n'
    output, report = repair(source, settings=settings(tmp_path), transport=transport('merge'))
    assert output == LEFT + ' ' + middle + ' ' + end + '\n\n' + first + '\n\n' + second + '\n'
    assert report['merged'] == 2


def test_capitalized_name_continuation_is_considered():
    left = LEFT + ' including the poet William Carlos'
    right = 'Williams, whose work emphasizes the visual properties of the line.'
    assert len(candidates(left + '\n\n' + right)) == 1


def test_short_prose_fragment_is_not_filtered_out():
    assert len(candidates(LEFT + '\n\n' + 'that this remains difficult.')) == 1


def test_keep_on_incomplete_grammar_is_reviewed(tmp_path):
    left = 'Cross-lingual Representation Similarity: We compare the sentence embeddings generated by the'
    right = 'native and romanized inputs as well as English on the base model. We compare their cosine similarities.'
    source = left + '\n\n![](table.png)\n\n' + right + '\n'
    calls = []
    def respond(request):
        payload = json.loads(request.content)
        calls.append(payload)
        decision = 'keep' if len(calls) == 1 else 'merge'
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
            'content': json.dumps({'decision': decision, 'reason': '复核断点'})}}]})
    output, report = repair(source, settings=settings(tmp_path), transport=httpx.MockTransport(respond))
    assert len(calls) == 2
    assert left + ' ' + right in output
    assert report['merged'] == 1


def test_complete_sentence_with_closing_parenthesis_is_not_a_break():
    left = LEFT + ' (this is a complete explanatory sentence.)'
    assert candidates(left + '\n\n' + RIGHT) == []


def test_affiliations_before_abstract_are_not_prose_candidates():
    source = '# Paper\n\n' + LEFT + '\n\n' + RIGHT + '\n\n# Abstract\n\nBody text.'
    assert candidates(source) == []


def test_one_word_continuation_can_complete_long_prose():
    assert len(candidates(LEFT + ' several unresolved research\n\nquestions.')) == 1


def test_short_poetry_lines_are_not_join_candidates():
    assert candidates('With thee\n\nO let me rise\n\nMost thinne.') == []


def test_appendix_table_caption_can_interrupt_a_sentence(tmp_path):
    block = 'Table A3: Dataset sizes\n\n<table><tr><td>100</td></tr></table>'
    source = LEFT + '\n\n' + block + '\n\n' + RIGHT + '\n'
    output, report = repair(source, settings=settings(tmp_path), transport=transport('merge'))
    assert report['merged'] == 1
    assert LEFT + ' ' + RIGHT in output


def test_paragraph_progress_reports_completed_checks(tmp_path):
    progress = []
    repair(SOURCE, settings=settings(tmp_path), transport=transport('keep'),
           on_progress=lambda done, total: progress.append((done, total)))
    assert progress == [(0, 1), (1, 1)]
