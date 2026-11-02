from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]


def test_office_motion_uses_known_fresh_state_and_exact_new_handoffs():
    code=r"""
const assert=require('node:assert/strict');
const F=require('./static/office-floor.js');
const a={agent_id:'a',generation:2,heartbeat_state:'fresh',turn_state:'running',state:'active'};
const b={...a,agent_id:'b'};
assert.equal(F.seatState(a,false,true),'typing');
assert.equal(F.seatState({...a,heartbeat_state:'stale'},false,true),'stale');
assert.equal(F.seatState({...a,heartbeat_state:'unknown'},false,true),'unknown');
assert.equal(F.seatState(a,true,true),'paused');
assert.equal(F.seatState(a,false,false),'unknown');
assert.equal(F.seatState({...a,turn_state:'waiting_model'},false,true),'waiting');
assert.equal(F.seatState({...a,turn_state:null},false,true),'unknown');
assert.equal(F.money(0),'$0.00');assert.equal(F.money(null),'Unknown');assert.equal(F.money(NaN),'Unknown');
const edge={event_id:'e',at:99,from_actor_id:'a',to_actor_id:'b',from_record_kind:'memo',from_record_id:'r',to_record_kind:'decision',to_record_id:'d'};
const s={health:{guard:{state:'running',read_state:'known',freshness:'fresh'}},clock:{state:'known',now:100},agents:{items:[a,b]},runtime_handoffs:{state:'known',items:[edge]}};
assert.deepEqual(F.newHandoffs(s,new Set(),false,false),[]);
assert.equal(F.newHandoffs(s,new Set(),true,false).length,1);
assert.deepEqual(F.newHandoffs(s,new Set(['e']),true,false),[]);
assert.deepEqual(F.newHandoffs(s,new Set(),true,true),[]);
s.agents.items[1]={...b,heartbeat_state:'unknown'};assert.deepEqual(F.newHandoffs(s,new Set(),true,false),[]);
s.agents.items[1]=b;s.runtime_handoffs.items=[{...edge,at:101}];assert.deepEqual(F.newHandoffs(s,new Set(),true,false),[]);
s.runtime_handoffs.items=[{...edge,from_record_id:null}];assert.deepEqual(F.newHandoffs(s,new Set(),true,false),[]);
s.runtime_handoffs.items=[{...edge,at:50}];assert.deepEqual(F.newHandoffs(s,new Set(),true,false),[]);
assert.equal(F.guardState({health:{guard:{state:'paused',freshness:'fresh',read_state:'known'}}}),'paused');
assert.equal(F.guardState({health:{guard:{state:'running',freshness:'stale',read_state:'known'}}}),'unknown');
"""
    r=subprocess.run(['node','-e',code],cwd=ROOT,text=True,capture_output=True,timeout=10)
    assert r.returncode==0,r.stderr


def test_office_tab_uses_current_floor_without_legacy_panels():
    html=(ROOT/'templates/index.html').read_text()
    app=(ROOT/'static/app.js').read_text()
    assert html.count('data-tab="desk"')==2 and '>Office</span>' in html
    assert 'id="office-scene"' not in html and 'id="office-legacy"' not in html
    assert 'Desk paper cohorts and diagnostics' not in html
    assert 'id="desk-body"' not in html and 'id="desk-stamp"' not in html
    assert html.index('/static/office-floor.js')<html.index('/static/office-v2.js')
    assert 'officeView.load' in app
    assert (ROOT/'sniper/web/templates/office.html').exists()
    assert 'prefers-reduced-motion' in (ROOT/'static/office-floor.js').read_text()


def test_unknown_guard_and_unavailable_handoff_source_never_animate():
    code="""
const assert=require('node:assert/strict'),F=require('./static/office-floor.js');
const a={agent_id:'a',heartbeat_state:'fresh',turn_state:'running'},b={...a,agent_id:'b'};
const s={clock:{state:'known',now:100},agents:{items:[a,b]},runtime_handoffs:{state:'unknown',items:[{event_id:'e',at:99,from_actor_id:'a',to_actor_id:'b',from_record_kind:'memo',from_record_id:'r',to_record_kind:'decision',to_record_id:'d'}]}};
assert.deepEqual(F.newHandoffs(s,new Set(),true,false),[]);
s.runtime_handoffs.state='known';assert.deepEqual(F.newHandoffs(s,new Set(),true,false),[]);
"""
    r=subprocess.run(['node','-e',code],cwd=ROOT,text=True,capture_output=True,timeout=10)
    assert r.returncode==0,r.stderr


def test_canvas_bubble_wraps_words_and_marks_overflow():
    code="""
const assert=require('node:assert/strict'),F=require('./static/office-floor.js');
const lines=F.bubbleLines('Pool proof is ready. Check the receipt.',24);
assert.equal(lines.join(' '),'Pool proof is ready. Check the receipt.');
assert.ok(lines.every(x=>x.length<=24));
assert.ok(F.bubbleLines('x'.repeat(200),24).at(-1).endsWith('…'));
"""
    r=subprocess.run(['node','-e',code],cwd=ROOT,text=True,capture_output=True,timeout=10)
    assert r.returncode==0,r.stderr
