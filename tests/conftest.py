"""Keep tests independent from the developer's real model credentials."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_translation_settings(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    monkeypatch.setenv(
        'PAPER_LIBRARY_SETTINGS_FILE',
        str(tmp_path / 'missing-paper-reader.env'),
    )
    monkeypatch.delenv('PAPER_TRANSLATION_PROFILE', raising=False)
    for name in tuple(__import__('os').environ):
        if name.startswith('PAPER_TRANSLATION_') and name not in {
            'PAPER_LIBRARY_SETTINGS_FILE',
        }:
            monkeypatch.delenv(name, raising=False)
