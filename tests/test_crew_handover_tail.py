import importlib
import json
import pytest


def module():return importlib.import_module('tools.crew.handover_tail')


def tail(tmp_path):
    path=tmp_path/'registered.jsonl';path.write_text('');path.chmod(0o600)
    return path,module().RegisteredTail(path,tmp_path/'cursor.json',allowlist=[path],version='verified',pinned_version='verified')


def test_only_new_complete_lines_and_restart_keeps_offset(tmp_path):
    path,reader=tail(tmp_path)
    assert reader.read()['records']==[]
    path.write_text('{"event":1}\n{"partial":')
    result=reader.read();assert result['records']==[{'event':1}]
    reader=module().RegisteredTail(path,tmp_path/'cursor.json',allowlist=[path],version='verified',pinned_version='verified')
    with path.open('a') as f:f.write('2}\n')
    assert reader.read()['records']==[{'partial':2}]
    assert reader.read()['records']==[]


@pytest.mark.parametrize('change',['rotate','truncate','malformed','oversize'])
def test_changed_or_invalid_stream_is_unknown_and_does_not_rebase(tmp_path,change):
    path,reader=tail(tmp_path);reader.read()
    path.write_text('{"event":1}\n');reader.read()
    if change=='rotate':
        path.rename(tmp_path/'old');path.write_text('{"new":1}\n');path.chmod(0o600)
    elif change=='truncate':path.write_text('')
    elif change=='malformed':
        with path.open('a') as f:f.write('not-json\n')
    else:
        with path.open('a') as f:f.write('x'*65536)
    assert reader.read()['state']=='unknown'
    assert reader.read()['state']=='unknown' and reader.read()['needs_page'] is True


def test_unregistered_or_unverified_parser_never_reads(tmp_path):
    path=tmp_path/'other';path.write_text('secret');path.chmod(0o600)
    with pytest.raises(ValueError):module().RegisteredTail(path,tmp_path/'cursor',allowlist=[],version='v',pinned_version='v')
    reader=module().RegisteredTail(path,tmp_path/'cursor',allowlist=[path],version='v',pinned_version='different')
    assert reader.read()['state']=='unknown' and not (tmp_path/'cursor').exists()


def test_first_attach_skips_history_and_same_inode_rewrite_is_unknown(tmp_path):
    path,reader=tail(tmp_path);path.write_text('{"inherited":1}\n')
    assert reader.read()['records']==[]
    path.write_text('{"rewritten":9}\n{"extra":2}\n')
    assert reader.read()['state']=='unknown'


def test_corrupt_cursor_is_typed_unknown_not_a_fresh_attachment(tmp_path):
    path,reader=tail(tmp_path);reader.read()
    (tmp_path/'cursor.json').write_text('{broken')
    assert reader.read()['state']=='unknown'
