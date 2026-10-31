from io import StringIO

import pytest

from paper_library.cli.progress import StructureProgress


class Terminal(StringIO):
    def isatty(self):
        return True


def test_terminal_progress_updates_in_place_and_closes_once():
    stream = Terminal()
    with StructureProgress(stream) as progress:
        progress.update(0, 218)
        progress.update(32, 218)
        progress.update(218, 218)
        assert '\n' not in stream.getvalue()
        assert '14%' in stream.getvalue()
        assert '100% 218/218 项' in stream.getvalue()
    progress.close()
    assert stream.getvalue().count('\n') == 1
    assert '\r\x1b[2K' in stream.getvalue()


def test_redirected_output_does_not_print_transient_updates():
    stream = StringIO()
    with StructureProgress(stream) as progress:
        progress.update(0, 218)
        progress.update(218, 218)
    assert stream.getvalue() == ''


def test_error_closes_line_without_claiming_completion():
    stream = Terminal()
    with pytest.raises(ValueError):
        with StructureProgress(stream) as progress:
            progress.update(32, 218)
            raise ValueError('failed')
    assert stream.getvalue().endswith('\n')
    assert '100%' not in stream.getvalue()


def test_empty_document_progress():
    stream = Terminal()
    with StructureProgress(stream) as progress:
        progress.update(0, 0)
    assert '100% 0/0 项' in stream.getvalue()
