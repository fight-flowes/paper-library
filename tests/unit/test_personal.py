import pytest
from paper_library.personal.store import PersonalStore
from paper_library.personal.text import TextMap,relocate


def test_notes_survive_source_changes_and_require_unique_location(tmp_path):
    store=PersonalStore(tmp_path/'personal.sqlite3')
    blocks={'source-1':'A sentence with 中文文本.'}
    note={'anchor':'source-1','quote':'中文文本','body':'我的笔记','document_version':'v1','kind':'highlight'}
    key=store.create('lib','paper',note,blocks,'v1')
    row=store.list('lib','paper')[0]
    assert relocate(row,{'source-8':blocks['source-1']},'v2')['resolved_anchor']=='source-8'
    assert relocate(row,{'source-8':'Gone'},'v2')['status']=='orphaned'
    assert relocate(row,{'a':blocks['source-1'],'b':blocks['source-1']},'v2')['status']=='orphaned'
    assert store.update('other','paper',key,'bad')==0
    assert store.list('lib','paper')[0]['body']=='我的笔记'
    with pytest.raises(ValueError):store.create('lib','paper',note,blocks,'v2')


def test_visible_text_keeps_anchor_and_translation_pair():
    text=TextMap('<p id="source-1">English <em>text</em><br>中文。</p><div data-translation-id="abc"></div>')
    assert text.blocks=={'source-1':'English text 中文。'}
    assert text.translations['abc']=='source-1'


def test_repeated_quote_uses_context_and_unicode(tmp_path):
    store=PersonalStore(tmp_path/'personal.sqlite3')
    text='first 🧪 中文 then second 🧪 中文 end'
    note={'anchor':'a','quote':'🧪 中文','prefix':'second ','suffix':' end','document_version':'v','body':'多语言笔记'}
    key=store.create('lib','paper',note,{'a':text},'v')
    row=store.list('lib','paper')[0]
    assert relocate(row,{'b':text},'new')['resolved_anchor']=='b'


def test_old_tab_cannot_overwrite_new_reading_position(tmp_path):
    from paper_library.personal.store import ProgressConflict
    first=PersonalStore(tmp_path/'personal.sqlite3');second=PersonalStore(first.path)
    data={'anchor':'a','quote':'text','document_version':'v','expected_revision':0,'percent':20}
    assert first.save_progress('lib','paper',data,{'a':'text'},'v')==1
    with pytest.raises(ProgressConflict):second.save_progress('lib','paper',{**data,'percent':5},{'a':'text'},'v')
    assert first.progress('lib','paper')['percent']==20
    assert second.save_progress('lib','paper',{**data,'expected_revision':1,'percent':40},{'a':'text'},'v')==2
