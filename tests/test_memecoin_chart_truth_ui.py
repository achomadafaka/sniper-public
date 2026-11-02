"""Check chart source identity, time intervals, and exit-price cache removal."""
import json
import re
import subprocess
from pathlib import Path

APP = Path('static/app.js').read_text()


# 2026-09-12: chart labels format prices through `_tokenPrice`; the harness object needs it.
TOKEN_PRICE = APP[APP.index('    _tokenPrice('):APP.index('    _operatorTime(')]
# 2026-09-28: a price with a known supply reads as a market cap (_supplyOf / _mcapAt / _pxOrMcap).
MCAP = APP[APP.index('    _supplyOf('):APP.index('    _fmtAgo(')]


def test_chart_keeps_sources_and_real_time_intervals():
    start = APP.index('    async _renderInlineChart(')
    end = APP.index('    _scheduleInlineChartRefresh(', start)
    local_start = APP.index('    _markChartView(') if '    _markChartView(' in APP else start
    helpers = APP[local_start:start] if local_start < start else ''
    script = '''
const esc = s => String(s);
const box = {innerHTML: '', querySelector() {return null;}};
global.document = {getElementById() {return box;}};
const fetchJson = async () => ({points: [
 {t:'2026-09-11T10:00:00Z', mid:1, source:'dexscreener', quality:'ok'},
 {t:'2026-09-11T10:00:10Z', mid:2, source:'dexscreener', quality:'ok'},
 {t:'2026-09-11T10:00:20Z', mid:0.5, source:'fastlane_rpc', quality:'exact_route'},
 {t:'2026-09-11T10:01:40Z', mid:3, source:'dexscreener', quality:'ok'}]});
const view = {_scheduleInlineChartRefresh() {}, _now() {return Date.parse('2026-09-11T10:01:42Z');},
''' + TOKEN_PRICE + MCAP + helpers + APP[start:end] + '''};
view._renderInlineChart('mint', 'chart', '4663').then(() => console.log(JSON.stringify(box.innerHTML)));
'''
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, check=True)
    html = json.loads(result.stdout)
    assert 'dexscreener' in html and 'market mark' in html
    assert 'fastlane_rpc · exact route quote' in html
    assert '2026-09-11 10:00:00' in html and '2026-09-11 10:01:40' in html
    assert 'window change' in html
    assert 'M6.0' in html and 'L64.8' in html
    assert 'fastlane_rpc' in html
    paths = re.findall(r'<path d="([^"]+)"', html)
    assert len(paths) == 2
    assert paths[0].count('L') == 2 and paths[1].count('L') == 0


def test_chart_separates_price_bases_within_a_source_and_keeps_unknown_unknown():
    """A source name cannot turn an executable quote or untyped price into a spot mark."""
    start = APP.index('    _markChartView(')
    end = APP.index('    async _renderInlineChart(', start)
    script = '''
const esc = s => String(s);
const box = {innerHTML:'', querySelector() {return null;}};
const view = {_now() {return Date.parse('2026-09-11T10:01:00Z');},
''' + TOKEN_PRICE + MCAP + APP[start:end] + '''};
view._markChartView(box, {points:[
 {t:'2026-09-11T10:00:00Z',mid:1,source:'same_source',quality:'live'},
 {t:'2026-09-11T10:00:10Z',mid:0.8,source:'same_source',quality:'exact_route'},
 {t:'2026-09-11T10:00:20Z',mid:1.2,source:'same_source',quality:'pool_reserves'},
 {t:'2026-09-11T10:00:30Z',mid:0.9,source:'same_source',quality:'exact_route'},
 {t:'2026-09-11T10:00:40Z',mid:1.1,source:'same_source'},
 {t:'2026-09-11T10:00:50Z',mid:1.3,source:'same_source',quality:'unrecognized'}]}, 110);
console.log(JSON.stringify(box.innerHTML));
'''
    html = json.loads(subprocess.run(['node', '-e', script], capture_output=True,
                                     text=True, check=True).stdout)
    assert 'same_source · market mark' in html
    assert 'same_source · exact route quote' in html
    assert 'same_source · price basis unknown' in html
    paths = re.findall(r'<path d="([^"]+)"', html)
    assert len(paths) == 3
    assert all(path.count('L') == 1 for path in paths)
    assert 'same_source · exact route quote · $0.8' in html
    assert 'same_source · price basis unknown · $1.10' in html


def test_missing_exit_tuple_does_not_reuse_previous_price():
    start = APP.index('    _mergeLastKnown(p) {')
    end = APP.index('    _realTable(', start)
    script = 'const view = {' + APP[start:end] + '''};
view._mergeLastKnown({id:1, selected_exit_price:3, selected_exit_basis:'measured_route', selected_exit_at:'old'});
console.log(JSON.stringify(view._mergeLastKnown({id:1, selected_exit_price:null, selected_exit_basis:'mid', selected_exit_at:null})));
'''
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, check=True)
    value = json.loads(result.stdout)
    assert value.get('selected_exit_price') is None
    assert value.get('selected_exit_at') is None


def test_chart_keeps_gaps_and_reports_the_observed_flat_range():
    start = APP.index('    _markChartView(')
    end = APP.index('    async _renderInlineChart(', start)
    script = '''
const esc = s => String(s);
const box = {innerHTML:'', querySelector() {return null;}};
const view = {_now() {return Date.parse('2026-09-11T10:10:00Z');},
''' + TOKEN_PRICE + MCAP + APP[start:end] + '''};
view._markChartView(box, {points:[
 {t:'bad',mid:8,source:'dexscreener'},
 {t:'2026-09-11T10:00:00Z',mid:1,source:'dexscreener'},
 {t:'2026-09-11T10:10:00Z',mid:1,source:'dexscreener'}]}, 110);
console.log(JSON.stringify(box.innerHTML));
'''
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, check=True)
    html = json.loads(result.stdout)
    assert 'range $1.00–$1.00' in html
    assert 'M6.0 55.0 M594.0 55.0' in html
    assert '2 marks' in html
