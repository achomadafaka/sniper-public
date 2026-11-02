"""Per-book open bags on the live Office view (floor board + standalone /office): typed from s.open_bags only."""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _node(code):
    r = subprocess.run(['node', '-e', code], cwd=ROOT, text=True, capture_output=True, timeout=10)
    assert r.returncode == 0, r.stderr


def test_floor_bag_rows_known_zero_null_age_unknown_and_limited():
    _node(r"""
const assert=require('node:assert/strict');
const F=require('./static/office-floor.js');
const known=(items,limited=false)=>({open_bags:{state:'known',items,limited}});
let r=F.bagRows(known([{book_id:'e:pod1',open_n:0,oldest_age_s:null,unpriced_n:0}]));
assert.equal(r[0].title,'Open bags · e:pod1');
assert.ok(r[0].detail.includes('0 open') && r[0].detail.includes('0 not priced'), r[0].detail);   // literal 0 stays 0
assert.ok(r[0].detail.includes('oldest Unknown'));                                                 // NULL age
r=F.bagRows(known([{book_id:'e:pod2',open_n:2,oldest_age_s:7200,unpriced_n:1}]));
assert.ok(r[0].detail.includes('2 open · oldest 2.0h · 1 not priced'), r[0].detail);
assert.deepEqual(F.bagRows({}).map(x=>x.detail),['Unknown · not readable in this read.']);        // missing source
assert.deepEqual(F.bagRows({open_bags:{state:'unknown',items:[],limited:false}}).map(x=>x.detail),['Unknown · not readable in this read.']);
assert.equal(F.bagRows(known([]))[0].detail,'None in loaded window.');
assert.ok(F.bagRows(known([],true))[0].detail.includes('truncated'));
assert.ok(F.bagRows(known([{book_id:'b',open_n:1,oldest_age_s:60,unpriced_n:0}],true))[0].detail.startsWith('loaded window truncated, at least 1 open'));
assert.ok(F.bagRows(known([{book_id:'b',open_n:'x',oldest_age_s:60,unpriced_n:null}]))[0].detail.includes('Unknown open'));
const many=Array.from({length:13},(_,i)=>({book_id:'b'+i,open_n:1,oldest_age_s:60,unpriced_n:0}));
r=F.bagRows(known(many));assert.equal(r.length,12);assert.ok(r[0].detail.includes('first 12 books'),r[0].detail);
""")


def test_floor_bags_render_independently_of_equity_history():
    js = (ROOT / 'static/office-floor.js').read_text()
    body = js[js.index('function render()'):js.index('async function load')]
    assert '...bagRows(s)' in body and 'equityRows.length ?' in body      # rows appended, never joined to equity books
