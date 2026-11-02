"""Check that a stream reconnect refreshes the complete position view."""
import json
import subprocess
from pathlib import Path

APP = Path('static/app.js').read_text()


def test_stream_reconnect_invalidates_status_and_book_caches():
    start = APP.index('    _ensureRealStream() {')
    end = APP.index('    _notifyFill(', start)
    script = '''
global.EventSource = class {addEventListener() {}};
const view = {_realSlowTs:100, _realStatusTs:100, _cockpitDirty:false,
''' + APP[start:end] + '''};
view._ensureRealStream();
view._realStream.onopen();
console.log(JSON.stringify([view._cockpitDirty, view._realSlowTs, view._realStatusTs]));
'''
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == [True, 0, 0]


def test_empty_ticker_invalidates_a_stale_open_position():
    start = APP.index('    _applyRealTicker(payload) {')
    end = APP.index('    _ageRealMarks() {', start)
    script = '''
global.document = {getElementById() {return {querySelectorAll(){return [];}};}};
const view = {_realPositions:[{id:7}], _realSlowTs:100, _realStatusTs:100, _cockpitDirty:false,
''' + APP[APP.index('    _operatorPrice('):APP.index('    _realTable(')] + '_renderAttention() {},' + APP[start:end] + '''};
view._applyRealTicker({positions:[]});
console.log(JSON.stringify([view._cockpitDirty, view._realSlowTs, view._realStatusTs]));
'''
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == [True, 0, 0]


def test_cockpit_change_refreshes_entry_results():
    start = APP.index('    _ensureRealStream() {')
    end = APP.index('    _notifyFill(', start)
    script = '''
global.EventSource = class { constructor(){this.handlers={};} addEventListener(k,fn){this.handlers[k]=fn;} };
let reads=0;
const view={_loadEntries(){reads++;},
''' + APP[start:end] + '''};
view._ensureRealStream();
view._realStream.handlers.cockpit();
console.log(JSON.stringify([reads, view._cockpitDirty, view._realSlowTs]));
'''
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == [1, True, 0]
